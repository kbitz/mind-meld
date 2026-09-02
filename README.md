# Mind Meld

[![CI](https://github.com/kbitz/mind-meld/actions/workflows/ci.yml/badge.svg)](https://github.com/kbitz/mind-meld/actions/workflows/ci.yml)

Sync AI coding-agent context, skills, and gstack activity across Macs via iCloud Drive. End-to-end encrypted. Supports Claude Code, Codex, and Grok. `mm` maintains a `retro-fleet` skill link only for hosts that do not discover `~/.claude/skills` (Claude Code, Codex, and — until that source is retired — OpenCode; verified 2026-08-24). Grok 1.0.5 already loads that directory via its default-on Claude compatibility layer, so `mm diag` reports Grok under `host_skill_discovery`, not as a fourth `skill_links` row.

## Install

```bash
pipx install git+https://github.com/kbitz/mind-meld.git@latest
```

Not on PyPI — install straight from GitHub. The `@latest` ref is a branch the release workflow force-advances to each tagged release, so you always get the newest *released* version (never untagged work-in-progress off `main`) **and** plain `pipx upgrade` keeps working (see below).

## Upgrading

```bash
pipx upgrade mind-meld
```

That's it. Because the install tracks the moving `latest` branch (not a frozen tag), `pipx upgrade` re-resolves it to the newest release and lands it.

**Stuck on an old version?** If you ever installed or upgraded with the old `--force …@vX.Y.Z` form, your install is pinned to that exact tag — pipx re-resolves the frozen ref on every `pipx upgrade` and reports your current version as "latest" forever. A git tag never moves; a branch does. Run this once to switch onto the `latest` branch, after which plain `pipx upgrade mind-meld` works:

```bash
pipx install --force git+https://github.com/kbitz/mind-meld.git@latest
```

(This is exactly the command mm's auto-upgrade nudge prints.)

**Need to roll back?** Install a specific older release by tag:

```bash
pipx install --force git+https://github.com/kbitz/mind-meld.git@v0.12.20
```

Nothing on disk needs migrating — config, manifests, blobs, and the events log
are unchanged across the 0.12.x line, so a rollback is just a reinstall. Note
the caveat above applies: a pinned tag stops tracking `latest`, so re-run the
`@latest` command when you want to resume upgrades.

## Quick Start

```bash
mm init    # configure iCloud storage + passphrase
mm push    # upload configured agent context
mm pull    # download from another device
```

Config lives at `~/.config/mind-meld/config.toml` — not tied to your current directory. Install `mm` anywhere, run from anywhere; it always syncs the sources configured in your global config.

## Setting up a second (or third) Mac

1. `pipx install git+https://github.com/kbitz/mind-meld.git@latest` on the new machine.
2. `mm init` — point it at the **same iCloud folder** as your first Mac and enter the **same passphrase**. This registers the new device against the existing roster.
3. `mm pull` — downloads everything the other machine(s) have pushed.
4. `mm push` — uploads anything this machine has that the others don't.

Push and pull on each Mac over time and the state converges toward the union of all three. Nothing is destroyed:

- **`.jsonl` files and `MEMORY.md`** deep-merge by line-union (deduped, sorted by `ts`). Entries from all machines accumulate — this is why telemetry, learnings, and timeline files stay coherent across devices.
- **Other divergent files** use mtime-skip: if your local file is newer than the remote, pull leaves it alone. Otherwise the remote wins the canonical path and your local version is preserved as `<stem>.sync-conflict-<ts>-<device>.<ext>` sitting next to it (Syncthing convention). See [Handling conflicts](#handling-conflicts) below.
- **Deletions** propagate via tombstones in the manifest — delete a file on one machine, and `mm pull` on the others removes it cleanly.

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

Mind Meld includes `autopull` and `autopush` commands designed for Claude Code — they run silently, never prompt, and output a single line summary (or nothing if already in sync).

Add the following to your **global** `~/.claude/CLAUDE.md` to have Claude automatically sync at the start and end of each conversation:

```markdown
# Mind Meld

At the **start of each conversation**, run:

\`\`\`bash
mm autopull
\`\`\`

- **No output:** Already in sync. Continue silently.
- **Any output:** Tell the user what was synced.

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
- "Silent" means no chatter on the happy path. Load-bearing degradation warnings — corrupt-manifest recovery, "no sync sources" misconfig, durability fsync failure, per-file pull failures — still reach stderr as a single `mm: warning: ...` line so a wedged background sync surfaces instead of rotting. Autopush writes a `no-sources` breadcrumb (separate from `success`) when the config has no sync sources. Both auto commands also write a `degraded` breadcrumb (separate from `success`) when an otherwise-successful run lost data: autopull on fsync durability failure, corrupt peer manifest, unknown source from a peer, or per-file apply failure; autopush (v0.12.16) when the fleet-retro events tail failed, exceeded its walk budget, or published no token/skill data because the token cache was cold or locked. A dropped host-usage reader — Mind Meld isolates host readers, so a source it cannot read is declared and omitted from that row's coverage rather than deleting the others or publishing a silent partial total — is reported the same way in `mm status`, and costs optional fleet-retro analytics only, never content sync. The `detail` field enumerates which signals fired. `mm status` and any monitoring on top of it can catch both wedge and partial-degradation cases. The one wedge no breadcrumb can report is the command never running at all — an `ImportError` at module scope, say, which dies before typer's runner and writes nothing — so since v0.12.21 `mm status` also marks any autorun breadcrumb older than 48 hours as `stale — no autorun in Nh` instead of reporting the last `success` forever.
- Fleet-retro capture is best-effort and never blocks content sync. Git repository discovery gets a small independent time budget; if it expires, autopush records a `degraded` breadcrumb and prints `mm: notice: git repository discovery hit its time budget: this push captured an incomplete repository set. Run mm diag, then mm recapture 30d to recover the omitted commits`. The detail is deliberately generic: it never exposes local paths or probe errors. Do not retry an empty push—the events tail intentionally runs only after a substantive sync change. A later ordinary push does **not** recapture the omitted interval; `mm recapture 30d` on the Mac that owns the repositories does. A healthy no-op autopush can still refresh its local autorun breadcrumb, which proves the hook ran; it does **not** mean fleet retro received a new activity event.
- **Auto-upgrade nudge (v0.9.5).** Once per 24h, `mm pull` / `mm push` (including the autopull/autopush variants) check GitHub for a newer release tag and emit a single `mm: notice: <old> → <new> available — run pipx install --force git+...@latest` line on stderr if you're behind. `mm` never invokes pipx itself; you run the printed command. The command tracks the moving `latest` branch (not a frozen tag), so it always lands the newest release and — crucially — rewrites any previously tag-pinned install's recorded URL onto `@latest`, after which plain `pipx upgrade mind-meld` works (see [Upgrading](#upgrading)). Disable with `--no-check-version` for one invocation, or set `[upgrade] auto_check = false` in `~/.config/mind-meld/config.toml` to disable persistently. The `notice:` prefix is distinct from `warning:` (reserved for data-at-risk signals). This is a leading-edge complement to the v0.9.2 fleet-version refusal, which only fires after a newer peer pushes data — the nudge fires before that, ideally making the refusal a backstop nobody hits.

## Codex Integration

Mind Meld treats Codex as a first-class peer of Claude Code. Grok is the other usage-reader host (see [Grok usage in fleet retro](#grok-usage-in-fleet-retro)). An `opencode` sync source still ships in the default config — `mm init` still asks about it and `mm enable-source opencode` still enables file sync — but OpenCode is no longer a usage-reader host.

- `codex` syncs `~/.codex/AGENTS.md`, `skills/`, and `plugins/`.
- `opencode` still syncs `~/.config/opencode/` customizations: rules, agents, commands, modes, plugins, skills, and tools. That source remains until it is retired; do not add it on a new machine expecting usage capture.
- Account credentials, session databases, logs, tool output, and whole-file `config.toml` / `opencode.json{,c}` settings are not sync sources. Those settings can contain inline provider or MCP credentials, so they stay local until Mind Meld can safely filter individual fields.
- Enabling the `codex` source also lets the fleet-retro capture read that host's local usage records to publish **aggregate token counts per day** (no prompts, transcripts, paths, or tool output ever leave the machine). Decline the source and its records are never opened. By default that also stops maintenance of a skill link in that host's skills directory; an explicit `[skills] agents` grant is the deliberate exception. `mm enable-source grok` is the same verb: it adds a scoped Grok source (`skills/`, `commands/`, and `rules/` only — same idea as Claude's `memory/` + `todos/`) and opts this Mac into reading terminal token totals from local `updates.jsonl` records. Session files, prompts, and chat history stay on the Mac. Enabling `opencode` still syncs that host's customizations and still consents a skill link; it does not turn on a usage reader.
- The bundled `/retro-fleet` skill is installed for an agent when that agent's sync source is enabled — on `mm init`, `mm push`, and `mm install-skills`. To keep a link maintained without enabling sync or usage reading: `mm install-skills --agent <key>`. OpenCode's Claude compatibility remains useful for existing gstack skills, and its skill link still works even when compatibility is disabled.

Symlinks inside any sync source are local routing, not portable content: Mind Meld does not upload them and leaves existing local links untouched on pull, including dangling links and linked directories. A source root itself may be a symlink.

Fresh installs are asked about the `codex` and `opencode` sources during `mm init`. That choice also decides whether mm maintains a `retro-fleet` skill link in that agent's skills directory. Existing installations remain opt-in. `mm enable-source opencode` still succeeds (it is a shipped sync source, not yet retired) and does not authorize a usage reader:

```bash
mm enable-source codex
mm enable-source opencode
mm enable-source grok
```

### Grok usage in fleet retro

`mm enable-source grok` does two things: it syncs `~/.grok` `skills/`, `commands/`, and `rules/` (session files stay local), and it opts this Mac into reading terminal token totals from local `updates.jsonl`. Prompts never leave the Mac. The fleet retro's `AGENT LOGS` block then gains a `Grok models: seen on N days` line — a day count, not a token magnitude.

Upgrade is per Mac, and **upgrading is not enough**. On each Mac:

1. `mm enable-source grok`
2. One **interactive** `mm push` (the fast path — it may warm the Grok cache once). Autopush never warms and converges over about three pushes instead.
3. Verify: `mm status` should read `Grok usage capture: enabled; a prior scan completed successfully`, and `mm diag` should show `grok prior successful scan: yes`. Then `mm retro-fleet 7d`.

If a later Grok release changes the log format, that Mac drops Grok (declared on `mm status` / `mm diag` / push stderr) and keeps publishing Codex. Upgrade mm, or `mm disable-source grok` to stop retrying.

Then give each agent the same lifecycle contract. For Codex, add this to `~/.codex/AGENTS.md` (or merge it into your existing global guidance):

```markdown
# Mind Meld

At the start of each conversation, run `mm autopull`.
At the end of each completed task or conversation, run `mm autopush`.

If either command has output, summarize it for the user. If it is silent, continue silently.
```

OpenCode reads `~/.claude/CLAUDE.md` as its global fallback, so this works immediately when Claude compatibility is enabled. If you use `OPENCODE_DISABLE_CLAUDE_CODE*` or have `~/.config/opencode/AGENTS.md`, put the same block there. This makes every agent feed the same gstack and `mm-events` history used by `retro-fleet`; Claude-only session and token breakdowns remain explicitly labeled as Claude-only.

### Manual commands

| Command | Description |
|---------|-------------|
| `mm --version` | Print the installed version and exit |
| `mm init` | Configure device, storage path, passphrase |
| `mm push` | Push with verbose output |
| `mm recapture [WINDOW]` | Redo this Mac's git capture for WINDOW (default `30d`, same as `mm init`). Safe to re-run — commits dedup fleet-wide on (remote, sha). Partial recovery exits 4. Retros window by the COMMIT's date, not by when mm captured it |
| `mm pull` | Pull with verbose output |
| `mm pull --conflict-mode prompt` | Pick a winner per-file at pull time instead of auto keep-both |
| `mm pull --conflict-mode fail` | Preflight all files; exit 3 (no writes) if any would conflict — for CI |
| `mm status` | Show local vs remote state, plus the last `autopull` / `autopush` breadcrumb — flagged `stale` when nothing has auto-run in 48h. Prints one extra line when a `retro-fleet` skill link is broken |
| `mm diag` | Dump non-secret crypto, sync, breadcrumb, `retro-fleet` skill-link state, Grok `host_skill_discovery`, host-usage reader state (`host_usage`), git-root `discovery`, and recorded-vs-fresh `git_capture` for triage. Runs without a passphrase, and without a valid config. `--json` for machine-readable output. Top-level keys: `mm_version`, `config`, `crypto_init`, `root_salt_drift`, `sidecar`, `storage_inventory`, `last_autorun`, `skill_links`, `host_skill_discovery`, `host_usage`, `discovery`, `git_capture`. Each `skill_links` row includes `maintain_links` (`enabled` / `disabled (…)` / `unknown (config invalid: …)` / `unknown (policy not resolved)`). `host_skill_discovery` is not a skill-link row. |
| `mm devices` | List registered devices |
| `mm devices --format=json` | Same data as a JSON array on stdout — for scripting (used by `/retro-fleet`) |
| `mm diff` | Dry-run: show what would change (annotates each file with write / merge / skip / conflict) |
| `mm gc` | Delete orphaned blobs and run local retention cleanup |
| `mm gc --dry-run` | Preview orphan blobs plus temporary, events, and token-cache retention cleanup without deleting; each reaper reports candidates and any repairs or skips |
| `mm gc --conflicts` | Also delete `.sync-conflict-*` files older than 30 days |
| `mm sources` | List configured sync sources |
| `mm log` | Query the per-file pull/push history. Filter with `--source`, `--since`, `--action {written\|merged\|skipped\|conflicted\|excluded\|uploaded\|failed}`, `--verb {pull\|push}`, `--limit`; `--format {jsonl\|table}` |
| `mm migrate-config` | Append any missing recommended `exclude_patterns` to your existing `[[sync.sources]]` entries. Idempotent and preserves your customizations; `--dry-run` to preview, `--yes` to skip the prompt |
| `mm refresh-identity` | Force-refresh the cached author-email set that decides which fleet commits count as yours. `--json` prints the resolved set |
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

This covers the common cross-machine cases — in particular, `/retro global` sees activity from all your Macs because `analytics/skill-usage.jsonl`, `analytics/eureka.jsonl`, and `projects/<slug>/timeline.jsonl` are all `.jsonl` files that **set-union merge** on pull (deduped, sorted by `ts`). Append-only telemetry from 3 machines converges cleanly into one timeline.

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

Grok is not a skill-link row. Verified 2026-08-24: Grok 1.0.5 discovers `~/.claude/skills` at the same documented priority tier as `~/.grok/skills` (`grok inspect --json`, then `name == "retro-fleet"`). Codex shows no evidence of that discovery (`~/.codex/AGENTS.md` mentions are the user's own instruction prose — and gstack's `setup --host auto` plus `machine-setup` *copy into* that dir, which you would not do for a dir the host already reads). An OpenCode skill link still exists until that source is retired; `~/.config/opencode/opencode.jsonc`'s `"~/.claude/**": "allow"` is a permission rule, not skill discovery. Exit criterion: **mm maintains a skill link only for hosts that do not discover `~/.claude/skills`.** Re-check with `grok inspect --json`.

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

Mind Meld v0.11.0 ships a Claude Code skill that stitches engineering activity from every Mac in your fleet into one accurate retrospective. Every substantive `mm push` writes a per-device daily JSONL row (commit metadata, sessions count, sync activity) to the synced `mm-events` source, so any machine can read the union and produce a fleet-wide picture. `mm recapture` writes extra git-snapshot rows (not an mm-push) to recover omitted intervals, then runs an ordinary push.

Inside Claude Code:

```text
/retro-fleet 7d     # last week, default if you omit the window
/retro-fleet 30d    # last month
/retro-fleet 90d    # last quarter (the retention ceiling)
```

The skill renders a paste-ready markdown retro — drop it into iMessage, Slack, or email. Commits are deduped across machines via `(canonical remote URL, sha)` so the same PR landed once but pushed from two laptops counts as one.

**v0.12.37 output shape.** A pixel-aligned ASCII card sits at the top, then the full markdown body (commit-type mix, peak hours, commit bursts, ship-of-the-window, weekly buckets when window ≥14d). The card carries, in order: volume, LOC, PR references, a `MODELS (Claude Code sessions)` block of per-family token totals, an `AGENT LOGS` block, then NOTEWORTHY and up to three TOP WORK bullets the skill synthesizes.

`AGENT LOGS` reports **rhythm, not magnitude** for other coding agents (Codex, Grok, and legacy OpenCode peers) — `Codex models: seen on 5 days`, plus `N of M machines with agent activity`. Rows are canonical model families rather than agents, because the synced snapshot carries no reader-to-family attribution; the day count is a lower bound, because a machine that has not pushed contributes no days and a peer on an older mm still reports last-touch totals. Per-machine token counters live in the body's `## Agent activity` table and are never summed across machines. Requires `mm enable-source codex` (or `grok`) on each machine — that opt-in is also what authorizes the local usage reader — and a `mm push` afterwards. When the block is quiet, `## Notes` names the cause and the fix rather than leaving an absence to be read as zero.

The card is generated via a two-pass flow: the first invocation emits an `MM_THEMES_PROMPT` JSON sidecar, the skill synthesizes themes + noteworthy, then re-invokes `mm retro-fleet <window> --theme … --noteworthy … --name …` to render the final card. `## Trends vs prior <N>d` is a two-column table computed from the synced events corpus (the immediately preceding equal-length window), identical in both passes, and fleet-deterministic — it does not depend on when you last typed the command. Direct CLI users (no skill) get the body without the card. The window argument is `Nd` — `7d`, not `7`.

Under the hood the skill invokes `mm retro-fleet <window>` (v0.11.22+) — the same CLI surface is available directly for scripted exports (`mm retro-fleet 30d > /tmp/retro.md`) or terminal use, just without the LLM judgment layer the skill adds (natural-language window parsing, error translation). The earlier `python -m mind_meld.skills.retro_fleet.aggregator` form is a development-checkout fallback only; pipx-installed mm lives in an isolated venv that bare `python` / `python3` can't import from, so the skill's documented invocation routes through the `mm` console-script (always on PATH wherever mm is installed).

**Token usage and API list-rate equivalent (v0.11.14, hosts in v0.12.52).** Under **Claude Code activity** the retro answers: how much did Claude Code consume this window, was it Sonnet- or Opus-heavy, did the cache do its job, what would this have cost at API list rates. Those numbers come from `~/.claude/projects/<encoded>/*.jsonl` plus subagent jsonls under `<session-uuid>/subagents/agent-*.jsonl` (subagents contribute to the parent project's totals — ~50% of usage on a heavy fleet — but don't double-count as separate sessions). The Claude cache lives at `~/.config/mind-meld/session-tokens.json`, warms inline on `mm init` and the first interactive `mm push` (~3 seconds, telegraphed via `mm: warming token cache (one-time, ~3s)...`), and is reaped by `mm gc` once a jsonl disappears or its tokens are older than 90 days.

From v0.12.52 the body also has **`## API list-rate equivalent (per machine)`** for the four observed `gpt-*` host models (and, later, Grok). It is not subscription spend: all three hosts on this fleet are subscription products, and the figure is today's short-context list rate applied to historical tokens. Sample:

```text
## API list-rate equivalent (per machine)

OpenAI short-context list rates, verified 2026-09-01 against
https://developers.openai.com/api/docs/pricing. …
### Do not sum these values
Machines may hold duplicated history … and these values must not be summed.

| Machine   | API list-rate equivalent |
|-----------|--------------------------|
| 3a6c7dc9  | ~$1,269                  |
| 889e42c0  | —                        |
```

Legend: `~` is an estimate over complete priced data; `>=` is a floor (unpriced models, a host that declared totals incomplete, a dropped reader, or tokens the per-day model cap left unattributed — Notes names which); `—` is unavailable, not zero. An all-unpriced device therefore shows `>=$0.00` plus the named cause, while a snapshot that predates the window shows `—`. A Mac on mm older than v0.12.52 reported inclusive token counters that would read up to ~2x high, so its row is also `—` until that Mac upgrades and re-pushes. The table shows estimates before unavailable rows when its 12-machine display cap applies, and states how many machines were omitted.

`--dump-host-usage` is the structured equivalent: it already carries `tokens_by_day`, so a script can compute the same number. Host totals never enter the Claude cost line, and there is no fleet sum.

**Rate provenance.** Anthropic list rates: `PRICING_LAST_UPDATED` in `token_usage.py`, verified against Anthropic's public pricing page. OpenAI short-context Standard: `PRICING_OPENAI_LAST_UPDATED`, verified against https://developers.openai.com/api/docs/pricing. Grok / xAI rates are held until Grok ingestion is proven. mm has no network, so a rate change is a code change.

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

If you edit the same file on two machines before syncing, `mm pull` never destroys your local edits. It follows the Syncthing convention: the incoming remote version wins the canonical path, and your local version is preserved as `<stem>.sync-conflict-<YYYYMMDD-HHMMSS>-<device>.<ext>` sitting next to it. If your local file is newer than the remote (by mtime), pull leaves it alone — convergence happens on the next push.

Managing conflicts:

- `mm conflicts` — list every `.sync-conflict-*` file across your sources, with age and canonical sibling.
- `mm resolve` — walk each conflict interactively. Shows color LOCAL/REMOTE banners (with peer-name attribution when the conflict file's device prefix matches a registered peer), created/modified timestamps for each side plus a `-> SIDE is newer by N` recency verdict, a 3-number divergence summary, the unified diff, and prompts: `(m)erge` (accept LCS-merged result) / `(l)ocal` (keep your edits) / `(r)emote` (overwrite with peer's bytes) / `(n)ewer` (keep whichever was modified more recently) / `(p)romote` (keep BOTH — give the conflict file its own first-class filename) / `(s)kip` (leave both files) / `(a)bort` (stop the walk). The default key is always `(s)kip` — Enter never auto-accepts a merge or a recency guess. The merge uses LCS(local, remote) as a synthetic ancestor so additive edits on either side land cleanly; same-region edits show as `<<<<<<<` markers and (m) stays available. Binary content suppresses (m); `(n)ewer` is offered only when both sides' mtimes are readable and re-prompts on an exact tie (it never guesses). The remote side's "created" is shown as `pulled` — it is the local sync time, not the peer's real creation (the manifest carries only modified time). Acquires the mm lockfile so autopull can't race your decision. Pre-1.0 letters `b` / `both` are aliased to `(s)kip` with a one-time stderr notice.
- `mm pull --conflict-mode prompt` — prompt per-conflict during the pull itself instead of auto keep-both. Shows the same per-side timestamps + recency verdict (display only — no `(n)ewer` shortcut here, since pull already keeps your file when it is the newer one).
- `mm pull --conflict-mode fail` — preflight all files; if any would conflict, print the list and exit 3 (no writes) so CI can block on human review. Exit 3 is distinct from typer's usage-error exit 2, so a stale script still passing the removed `--no-prompt` flag can't be mistaken for a conflict refusal.
- `mm gc --conflicts` — reap stale conflict files older than 30 days.
- `mm diff` — predicts each modified file's pull outcome (write / merge / skip / conflict) before you run pull.

## Troubleshooting

**Retro output is missing a block, unexpectedly empty, or older than expected.** Treat the missing data as unknown, not zero. Run `command -v mm`, `mm --version`, `mm diag`, and an interactive `mm push`. If `mm status` or `mm diag` shows an incomplete git capture, recover on that Mac with `mm recapture 30d`, then rerun the retro at a window that includes the recovered commit dates. If push prints an upgrade notice, run its command, then run `mm install-skills` (or `mm install-skills --agent KEY` if `mm diag` shows that agent as `maintain_links: disabled`), **restart the agent**, and rerun the retro. Bare `mm install-skills` skips agents not authorized by the current `[skills]` policy; by default that means sources you declined. If `mm push` fails, its error explains which local data was not refreshed. This cannot tell you whether the SKILL.md the agent loaded matches the store copy — only that the binary and the published store are what they are.

**Why is my host cost missing (`—` on the economics table)?** That Mac reported token counters in an older format (mm < v0.12.52), has not pushed per-model `tokens_by_day` yet (mm < v0.12.49), or its latest snapshot predates the requested window. On **that** Mac: `pipx upgrade mind-meld`, then an interactive `mm push`. Confirm with `mm diag` (Host usage block, `host counter format`). Then re-run `mm retro-fleet 30d` here. An upgraded peer's retained 90 days generally **do** become priceable on repush. `—` is unavailable, not zero; do not add the other machines' figures to fill it in.

**I enabled Grok, but no Grok activity appears.** Upgrade is not enough — run `mm enable-source grok` on that Mac, then one interactive `mm push`. `mm status` reports outcome, not config: `enabled; a prior scan completed successfully` vs `enabled, but no successful scan yet`. `mm diag` shows consent, whether a prior scan completed, and how many usage-less turns were skipped, without opening `~/.grok/sessions`. If push stderr names `grok` with `unsupported`, the log format changed; upgrade mm, or `mm disable-source grok` to stop retrying. Codex totals are unaffected. Grok API-list-rate figures are not in v0.12.52: ingestion is not yet proven on this fleet.

**`mm` is not on PATH after install.** pipx puts console scripts in `~/.local/bin`. If a Homebrew-installed `mm` shadows it, `which -a mm` shows both — fix the PATH order rather than deleting either.

**`mm --version` reports an old version after `pipx upgrade`.** Your install is pinned to a frozen tag. See [Upgrading](#upgrading) for the one-line fix.

**`/retro-fleet` is missing from an agent, or the agent sees a dead skill entry.** Run `mm diag` — it prints one row per agent with the link's status, its `maintain_links` policy, plus its `readlink` target (or, when there is no link to read, the reason), and needs no passphrase and no valid config. `mm diag --json` top-level keys are `mm_version`, `config`, `crypto_init`, `root_salt_drift`, `sidecar`, `storage_inventory`, `last_autorun`, `skill_links`, `host_skill_discovery`, `host_usage`, `discovery`, and `git_capture`. Each `skill_links` row carries `key`, `agent`, `target`, `store`, `store_state`, `status`, and `maintain_links`, plus `store_version` on rows that were diagnosed successfully — the defensive `status: "error"` row omits it, so read that field defensively. When the config cannot be parsed, `maintain_links` is `unknown (config invalid: …)`, never `disabled`. A bare diagnose with no policy set is `unknown (policy not resolved)`. `host_skill_discovery` (Grok only) carries `host`, `status`, `claude_skills_compat`, `retro_fleet_resolved`, `retro_fleet_path`, and `grok_version`. It is not a fourth `skill_links` row: a probe result is not a link mm owns. `status` there is one of `ok`, `binary-absent`, `timeout`, `nonzero-exit`, `malformed-json`, `unsupported-schema`. It does not carry the SKILL.md the agent loaded, the resolved `mm` path, or whether an upgrade is available. Then run `mm install-skills`, which creates missing links and repairs Mind Meld's own dangling ones **for authorized agents**, and restart the agent so it reloads SKILL.md. If `maintain_links` is disabled, use `mm install-skills --agent KEY` to grant maintenance without enabling sync. If the row says `removed-by-user`, you deleted that link and `mm` is leaving it deleted — `mm install-skills` puts it back (see [Removing a skill link](#removing-a-skill-link)); `absent` means that agent never had one. If the row says the link is a file, or points somewhere Mind Meld does not recognize, that entry is yours: move it aside first, then re-run. `mm status` prints a one-line nag (cause + fix + restart) whenever a link is in a state it can call broken, so you don't have to remember to check — but a *live* entry of your own, and a declined row whose link still resolves, are not those states, so use `mm diag` when the skill is present and simply isn't Mind Meld's. For Grok, read `host_skill_discovery`, not `skill_links`. If `claude_skills_compat` is false, `[skills] ignore` is the documented lever that breaks discovery; `[skills] paths = ["~/.local/share/mind-meld/agent-skills"]` adds the store. The compat-off toggle itself is undocumented in Grok 1.0.5 — trust `grok inspect --json` → `externalCompat`.

**`/retro-fleet` refuses because `mm` is missing, or the leftover skill still offers the command after uninstall.** A current skill stops before running the retro and names a PATH-order check; an older skill (or leftover links after uninstall) still errors mid-run with `mm: command not found`. The skill store outlives `mm` (see [Uninstalling](#uninstalling)). Either reinstall `mm` or remove the leftover links.

**An agent's link is no longer repaired, and you never changed anything.** Nothing is broken. `mm` created that agent's link before 0.12.42 gated link maintenance on `[skills]` policy, and your current policy does not authorize that agent — by default that means its sync source is not enabled. It applies only where the link at the target still `readlink`s to `~/.local/share/mind-meld/agent-skills/retro-fleet`, which is what proves `mm` created it under the old ungated policy.

The link keeps working. Store publish is *not* consent-gated, so `SKILL.md` refreshes for every link still pointing at the store regardless of policy, and the agent keeps offering `/retro-fleet`. What stops is repair: if that link is deleted, clobbered, or left dangling by a future store change, `mm` will not recreate it and the agent loses the skill silently. To keep it maintained, run `mm install-skills --agent KEY` (repeatable) — it grants link maintenance without enabling source sync or usage reading. To accept the decline, do nothing; the link works until something breaks it.

Versions 0.12.42 and 0.12.43 announced this once on stderr; v0.12.44 removed that notice, because a machine that skips those releases never receives it and this entry is the durable explanation. A leftover `~/.config/mind-meld/.skill-link-policy-v0.12.42` marker is inert and safe to ignore. `mm diag` is the authoritative resolved view: a declined row whose link still resolves reads `status: ok` with `maintain_links: disabled`, and that pair is a deliberate decline, not a fault.

**`mm status` says `stale — no autorun in Nh`.** Nothing has run `mm autopull` / `mm autopush` in 48 hours, so your agent's lifecycle hook is not firing. Check the `# Mind Meld` block is still in the global instructions file that agent actually reads.

**`mm status` shows a `degraded` breadcrumb.** The sync itself succeeded; the `detail` field names which optional signal was lost. Fleet-retro capture and host-usage snapshots are best-effort and never block content sync. If the detail mentions git repository discovery, run `mm diag`, then `mm recapture 30d` on that Mac — a later ordinary push does not recapture the omitted interval.

**`mm retro-fleet` under-counts commits, or `mm diag` shows `status: empty` / `exceeded`.** Discovery is local to each Mac. Upgrade that machine, then `mm recapture 30d` (or `mm push` to capture going forward). Recapture does not change commit dates: verify with `mm retro-fleet` at a window that includes those dates. To force a machine to include a repo Claude Code has no session for, add it to `[retro] repo_roots` (absolute paths) and verify with `mm diag`.

**`mm push` prints `events tail budget exceeded`.** Run `mm gc` to reap token-cache entries for sessions that no longer exist, which shrinks what every push has to read.

**A file came back after you deleted it.** Deletions propagate via tombstones on the *next* push from the machine that deleted it. Push there, then pull elsewhere.

**Conflicts you didn't expect.** `mm conflicts` lists them, `mm diff` predicts them before a pull, and `mm resolve` walks them interactively. See [Handling conflicts](#handling-conflicts).

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
# including the retired OpenCode path. Track 37B leaves that link on disk
# rather than growing a reaper; this loop is the only cleanup. Do not
# delete the opencode path as a "prose sweep" — it is an explicit exception.
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
