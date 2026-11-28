# Mind Meld

[![CI](https://github.com/kbitz/mind-meld/actions/workflows/ci.yml/badge.svg)](https://github.com/kbitz/mind-meld/actions/workflows/ci.yml)

Mind Meld (`mm`) syncs your AI coding-agent context across macOS and Arch Linux. It encrypts your data before writing it to your shared Syncthing folder. Sync sources include **Claude Code**, **Codex**, **Grok Build**, **Muse**, **Antigravity**, **gstack**, and **gstack-extend**. Use **`/retro-fleet`** for usage across machines and **`qmd`** to search their memories locally.

## Install

On macOS, install [Homebrew](https://brew.sh/) first. On macOS or Arch Linux, run the bootstrap script from the `latest` release branch:

```bash
mm_bootstrap=$(curl -fsSL https://raw.githubusercontent.com/kbitz/mind-meld/latest/bin/bootstrap) && sh -c "$mm_bootstrap"
```

The script uses Homebrew on macOS or `pacman` on Arch to install missing Python 3.11+, `pipx`, and Syncthing 2.1+. It installs missing Git on Arch, installs or updates `mm` via `pipx`, and tries to install `qmd` via Bun or npm. With default Syncthing settings it enables Syncthing at login. If you already configure Syncthing yourself, follow the [manual setup](#configuring-syncthing-yourself).

For a manual install when you already have Python 3.11+, `pipx`, and Syncthing 2.1+:

```bash
pipx install git+https://github.com/kbitz/mind-meld.git@latest
```

The `@latest` branch tracks each tagged release, so `mm update` and `pipx upgrade mind-meld` always move to the newest release rather than untagged commits on `main`.

## Quick Start

The shared store lives at the root of one Syncthing folder (`~/Sync/mind-meld` by default). On Arch Linux, install `gnome-keyring` and `libsecret` and unlock your login keyring in the desktop session. If you sync Claude Code project memory, open Claude Code once before `mm init` so `~/.claude` exists.

```bash
# First machine of a new fleet (empty folder):
mm init --new-fleet      # sets up default Syncthing, writes key header + .stignore, prints Syncthing ID
mm push --dry-run        # preview: changes nothing except the lock file
mm push

# Joining machine:
mm init                  # asks start/join, saves answers, pairs with one fleet machine, waits for approval
# On the fleet machine whose ID you pasted, run the approval command printed by init.
# Use its Syncthing GUI if you manage Syncthing there yourself (see below).
# Back on the joining machine once init finishes pulling:
mm push

# Everyday workflow:
mm pull --dry-run   # preview: changes nothing except the lock file
mm pull
mm status
mm diag
```

Typical 1.12.9x output — one line per outcome, nothing for what didn't happen:

```text
$ mm push
Push: 1 content change published.

$ mm push
Push: 10 content changes published; usage published.

$ mm push
Push: no content changes; no new usage.

$ mm pull
Pull: 1 content change from mac-a.
```

## Setting up another machine

On a machine whose Syncthing still has its default configuration (no paired devices or custom folders, and any stock Default Folder is empty and unshared), `mm init` handles Syncthing setup automatically:

1. Run `mm init` and choose join (`j`). It asks for the storage path, one existing fleet machine's Syncthing device ID, this machine's name, sync sources, and optional `qmd` memory-index settings, then saves those answers (never the passphrase) in `~/.config/mind-meld/join.toml`.
2. `mm init` pairs with that fleet machine and prints this machine's Syncthing ID and copyable approval commands. The approval wait lasts about 10 minutes.
3. On the fleet machine, run the printed `mm devices approve <short-id>` or `mm devices approve <device-id>` command if `mm` manages Syncthing there. Compare the 7-character short ID before approving. For an owner-managed fleet machine, [approve through Syncthing](#configuring-syncthing-yourself).
4. Once approved, the joining machine accepts the folder, prompts for the fleet passphrase, verifies it, registers itself, publishes its encrypted `syncthing/<id>.enc` record, and pulls.

If the wait times out or you press Ctrl-C, `mm init` exits with `Run: mm init`. Rerunning offers to resume with your saved answers so you don't retype them.

**One approval connects the whole managed mesh.** Each machine publishes its own Syncthing device ID in encrypted `syncthing/<id>.enc`. On every other machine where `mm` manages Syncthing, the next `mm push`, `mm pull`, `mm autopush`, or `mm autopull` pairs knocking fleet peers vouched by those records, clears stale introducer flags, keeps device names aligned, and removes disconnected retired devices — no introducers required.

**Retiring a machine:** [Save its unpulled work and disconnect it first](#retiring-a-dead-machine), then run `mm devices retire <name|id>` on an active machine. It writes an encrypted `retired/<id>.enc` record and removes `syncthing/<id>.enc` and `devices/<id>.json`. Managed peers remove eligible disconnected devices on a later sync.

### Configuring Syncthing yourself

If a machine already has its own Syncthing configuration (paired devices, custom folders, or a non-empty Default Folder), `mm init` leaves Syncthing untouched. Configure Syncthing yourself:

1. [Pair](#pairing-machines-in-syncthing) with one fleet machine. On the **joining owner-managed machine**, mark that fleet machine as an **Introducer** so the joiner learns later peers. Leave the introducer setting off for the joiner on the fleet side.
2. On the fleet machine, share the folder with ID `mind-meld` with the joiner. On the owner-managed joiner, accept the share at `~/Sync/mind-meld` (or your chosen storage path) and apply the [folder options](#syncthing-folder-options).
3. Wait for `mm-crypto-init` to arrive, then run `mm init`, choose join, and enter the fleet passphrase when prompted.

If you're approving a join from an owner-managed **fleet machine**, accept the joining device in that machine's Syncthing GUI. Open the `mind-meld` folder's **Edit → Sharing** tab, select the joining device, and save. The waiting managed joiner then accepts the offer.

### Pairing machines in Syncthing

In the Syncthing GUI (`http://127.0.0.1:8384`), find a machine's ID under **Actions → Show ID**. Add the peer with **Add Remote Device**, share the `mind-meld` folder on its **Sharing** tab, and accept the incoming device and folder prompts on the other machine.

### Syncthing folder options

Share the storage root as its own folder. If you also share its parent `~/Sync`, exclude `/mind-meld` in the parent's `.stignore` on each machine that shares it. `mm status` and `mm diag` check the fleet folder settings:

- **Folder type:** Send & Receive
- **Ignore permissions (`ignorePerms`):** off, so mode `0600` survives
- **File versioning:** none (`mm` keeps its own 30-day deletion recovery copies)
- **Maximum conflicts (`maxConflicts`):** `-1` or any positive number so Syncthing keeps conflict copies
- **Pull order (`order`):** alphabetic, so blobs arrive before the manifests that reference them
- **`.stignore`:** add `(?d)tmp*.tmp` at the fleet folder root on every owner-managed machine. Managed setup writes the rule locally.

<a id="syncthing-mesh-causes"></a>
Check mesh problems with `mm status` and `mm diag`:

- **`unreachable`, `timeout`:** make sure local Syncthing is running. The next push or pull retries.
- **`unauthorized`, `config-unreadable`, `address-unsupported`, `path-unreadable`, `bad-response`:** run `mm diag` to check the local Syncthing configuration and API response.
- **`no-folder`, `not-mm-folder`:** check the `mind-meld` folder ID, storage path, and options above.
- **Unreadable or newer-format peer record:** run `mm diag` for an unreadable record, or `mm update` for a newer format.

An attended pull also explains pending join requests:

- **Knocking fleet machine without `syncthing/<id>.enc`:** another registered machine hasn't published its encrypted Syncthing record yet → run `mm update` and `mm push` on that machine.
- **Superseded old registration:** a re-initialized machine left an old entry → run `mm devices retire <old-id>`.
- **New joining machine:** run `mm devices approve`.

## Moving a machine from iCloud Drive to Syncthing

There is no automated migration tool. Move each Mac on its own schedule — Macs still on iCloud Drive keep syncing until you move them:

1. Close agent sessions so hooks stay idle. Optionally run one last `mm push` to iCloud. Back up `~/.config/mind-meld/config.toml` and any custom-excluded files outside your sync sources. A join pulls before you can reapply your exclusions.
2. Run `pipx uninstall mind-meld`. Keep `~/.config/mind-meld` for the pull ledger and recovery copies. Move `~/.local/share/mind-meld/events` outside the source, for example to `~/mind-meld-icloud-events`, to keep old event history out of the new fleet.
3. Reinstall with the [bootstrap script](#install). Run `mm init --new-fleet` for the first Syncthing machine or [join](#setting-up-another-machine) with `mm init`. Use the Syncthing path, confirm replacing the config, and decline previously disabled sources.
4. Reapply your settings and exclusions, restore excluded files from backup, and run `mm push`.

If init stops, keep the original backups. For an interrupted join, rerun `mm init` and use its saved answers. For a new fleet, retry `mm init --new-fleet` only if the folder is still empty of fleet data. If init already wrote `mm-crypto-init` but has yet to save the new config, run plain `mm init`, choose join, and use the same folder and passphrase. Once the Syncthing config is saved, continue at step 4.

Your agent files merge under the normal [pull conflict rules](#handling-conflicts). iCloud and Syncthing remain separate stores. Save the iCloud folder until every Mac has moved.

## Commands and output

### Manual commands

| Command | Description |
|---------|-------------|
| `mm --version` | Print the installed version and exit |
| `mm init` | Start a new fleet or join an existing one; sets up default Syncthing, saves join answers, waits for approval, verifies passphrase, pulls |
| `mm init --new-fleet` | First machine of a new fleet: configures default Syncthing, writes key header (`mm-crypto-init`) and `.stignore`, prints Syncthing device ID |
| `mm push` | Sync all enabled sources and refresh consented host usage; publishes `syncthing/<id>.enc` and runs the managed mesh pass. Exit 0 means content sync succeeded regardless of capture outcome |
| `mm push --dry-run` | Preview publication and deletions without uploading; changes nothing except the local lock file |
| `mm pull` | Pull peer changes, apply fast-forwards, merge `.jsonl` and `MEMORY.md`, keep conflict sidecars, propagate deletions with 30-day recovery copies, and run the managed mesh pass |
| `mm pull --dry-run` | Preview writes, merges, conflicts, deletions, and older edits (`--verbose` explains kept files); changes nothing except the local lock file |
| `mm pull --conflict-mode prompt` | Choose a winner interactively per conflict during pull |
| `mm pull --conflict-mode fail` | Exit 3 before applying any file if conflicts or local failures are predicted; add `--dry-run` for a write-free CI gate |
| `mm status` | Show a one-line verdict, Syncthing state, each peer's last push and applied state, latest auto/attended runs, and actionable diagnostics |
| `mm diag` | Print non-secret crypto, sync, Syncthing, keyring, skill-link, host-usage, discovery, `git_capture`, `memory_index`, `capture_catchup`, and `push_timings` state (`--json` for machine-readable output). Runs without a passphrase or valid config |
| `mm diff` | Compare local files with this Mac's last push (or `--from DEVICE`). Changes nothing. For incoming changes, use `mm pull --dry-run` |
| `mm devices` | List registered fleet machines and their last-seen versions (`--format=json` for scripting) |
| `mm devices approve [name\|short-id\|device-id]` | Approve a joining machine on an `mm`-managed fleet machine (bare form lists pending devices interactively on a terminal) |
| `mm devices retire <name\|id> [--yes]` | Retire a fleet machine, writing `retired/<id>.enc` and removing its device and Syncthing records |
| `mm sources` | List configured sync sources, enabled state, and file counts |
| `mm enable-source <name>` | Enable a sync source (and its host-usage consent / skill link where applicable) |
| `mm disable-source <name>` | Disable a sync source on this machine without deleting local files or writing tombstones |
| `mm reconfigure-sources` | Re-run the interactive source selection prompts |
| `mm conflicts` | List unresolved `.sync-conflict-*` files with age and canonical sibling |
| `mm resolve [PATH]` | Interactively resolve conflict files with a unified diff (requires a terminal) |
| `mm recover --abandon-manifest` | Last resort when push reports a corrupt manifest and no recovery source. Quarantines this machine's corrupt manifest and discards its unpublished deletion records. For a deleted file, use [the file recovery recipe](#recovering-a-deleted-file) |
| `mm gc` | Delete this device's orphaned blobs and run local retention cleanup (including 30-day deleted-file copies); `--dry-run` previews, `--conflicts` also reaps redundant conflict copies older than 30 days |
| `mm log` | Query local `~/.config/mind-meld/state.db` history. Filter with `--source`, `--since`, `--action {written\|merged\|merged-via-lcs\|skipped\|conflicted\|excluded\|uploaded\|failed\|deleted}`, `--verb {pull\|push\|self-upgrade}`, `--limit`; `--format {jsonl\|table}` |
| `mm memory-index [on\|off]` | Inspect or toggle the dedicated `mind-meld` qmd memory index (bare form is read-only) |
| `mm migrate-config` | Update legacy config entries and append missing recommended `exclude_patterns` / `include_patterns` (`--dry-run`, `--yes`) |
| `mm refresh-identity` | Force-refresh the cached git author-email set used by `retro-fleet` (`--json` prints the resolved set) |
| `mm recapture [WINDOW]` | Re-capture this machine's git commits for `WINDOW` (default `30d`) and push (`--dry-run`, `--verbose`; partial recovery exits 4) |
| `mm retro-fleet [WINDOW]` | Render the fleet retrospective markdown to stdout (default `7d`; `--no-author-filter`, `--dump-host-usage`, `--set-model-price`, `--mark-pricing-verified`) |
| `mm install-skills` | Install or repair `retro-fleet` skill links for authorized agents (`--agent KEY` grants link maintenance for one agent without enabling sync) |
| `mm update` | Update `mm` to the latest release via `pipx` (needs no config or passphrase) |
| `mm autopull` / `mm autopush` | Run pull or push from agent hooks without prompts |

### Sync outcomes

Every hand-run command uses one shared plain-text style:

- **Outcome (stdout):** `<Verb>: <what happened>.` — one line per thing that happened, nothing for what didn't (`Push: 10 content changes published; usage published.`, `Push: 1 file held.`, `Pull: 1 content change from mac-a, 2 content changes from mac-b (1 conflict copy).`).
- **Error (stderr):** `mm: error: <what broke>`, with `Run: <command>` on its own last line when a fix applies.
- **Warning (stderr):** `mm: warning: <what is wrong>` (data-at-risk warnings are never quiet-gated).
- **Notice (stderr):** `mm: notice: <what changed or was skipped>`.
- **Fix command:** `Run: <command>` on its own last line (CLI usage errors exit 2 with `Run: <command path> --help`).

On an interactive terminal (without `--verbose` or `--dry-run`), `mm push`, `mm pull`, and `mm update` show a transient spinner after 3 seconds with live phase labels, counts, and elapsed time, clearing before the final outcome lines.

**`Push:` usage clause (1.12.9x):**
- When an attended push publishes changed usage totals (host usage, Claude sessions, or Git commits) or queued catch-up data, the lead line appends `; usage published` (`Push: 10 content changes published; usage published.` or `Push: no content changes; usage published.`).
- When a content-changing push has no new usage, the usage clause is omitted (`Push: 1 content change published.`).
- When an idle push has no new usage, it prints `Push: no content changes; no new usage.`.
- If a host reader hit its deadline, the lead line appends the reader status (`(Grok catching up)`).
- A file still changing during scan/upload keeps its previously published version and adds `Push: N files held.`.
- `mm pull` reports applied changes per peer (`<n> content change(s) from <device>`), deletions with recovery folder, and any blobs still waiting on Syncthing delivery.

### Status verdicts

`mm status` opens with a one-line verdict (in sync, changes waiting, or the action needed), followed by Syncthing state, each peer's last push and whether this machine applied it (anything overdue > 24 h warns in every mode), the latest auto/attended run per verb, and current diagnostics. Any autorun breadcrumb older than 48 h is flagged `stale — no autorun in Nh`.

#### Run evidence diagnostics

| Code | Meaning and action |
|------|--------------------|
| <a id="run-evidence-write-unavailable"></a>`run-evidence-write-unavailable` | Sync succeeded, but recording local history failed. Restore local access/storage and run `mm diag`. |
| <a id="run-evidence-contended"></a>`run-evidence-contended` | History read was busy. Retry `mm status`; if it persists, run `mm diag`. Never delete a live lock. |
| <a id="run-evidence-corrupt"></a><a id="run-evidence-oversized"></a><a id="run-evidence-unreadable"></a>`run-evidence-corrupt`, `run-evidence-oversized`, `run-evidence-unreadable` | History state is unknown. Inspect with `mm diag`; do not delete or blindly retry. |
| <a id="run-evidence-ambiguous"></a><a id="run-evidence-limited-coverage"></a>`run-evidence-ambiguous`, `run-evidence-limited-coverage` | Ordering or scope cannot prove recovery. Inspect with `mm diag`. |

### Previews

Previews never mutate sync data, prompt for migration, check for updates, or repair crypto-init state.

| Command | May touch | Not previewed | Exit codes |
|---|---|---|---|
| `mm push --dry-run` | Local lock only | Host scanning/warming/capture, activity row, post-push GC, upload re-reads (scan-time holds are shown) | 0 completed, including held files; 1 stopped |
| `mm pull --dry-run` | Local lock only | Conflict renames, pull history, sync logs, blob download/decrypt failures, waiting blobs, pull ledger writes, recovery copies | 0 completed, including incomplete scans; 1 stopped; 3 when fail mode predicts conflicts or failures |
| `mm gc --dry-run` | Local lock only | Actual cleanup. Previews orphan blobs and retention candidates, including conflict copies (`--conflicts` required to delete those) | 0 completed; 1 stopped |
| `mm recapture --dry-run [WINDOW]` | Local lock only | Writing git-snapshot rows and pushing | 0 completed, including partial scans; 1 stopped or no repositories |
| `mm migrate-config --dry-run` | Nothing, including no lock | Applying the displayed config changes | 0 completed; 1 config error |
| `mm diff [--from DEVICE]` | Nothing, including no lock | Snapshot comparison, not a forecast of all incoming peers; use `mm pull --dry-run` for that | 0 completed; 1 stopped |

For a write-free CI gate, run `mm pull --dry-run --conflict-mode fail` (0 = no conflicts/failures predicted, 1 = stopped, 3 = conflicts or local failures predicted).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Completed successfully (`autopull`/`autopush` also exit 0 on graceful degradation; check stderr and `mm status`) |
| 1 | Stopped, interrupted, or incomplete (including attended pull with failed files, config/crypto/lock errors, invalid flag values) |
| 2 | CLI usage error (Typer/Click or `retro-fleet` argparse) |
| 3 | `pull --conflict-mode fail` predicted a conflict or local failure (including with `--dry-run`) |
| 4 | `mm recapture` completed only partially |

## Project memory across machines

Claude Code project memory (`~/.claude/projects/<encoded>/memory/` and `todos/`) is mapped across machines by each repository's canonical git `origin` remote — so a repo at `/Users/user/code/app` on macOS and `/home/user/src/app` on Linux shares one project memory automatically. A project without a network `origin` falls back to its home-relative path when it lives under `$HOME`; projects outside `$HOME` with no `origin` stay machine-specific.

## Claude Code Integration

`mm autopull` and `mm autopush` are designed for agent hooks: they never prompt, stay silent on the happy path, and degrade gracefully on errors. If a locked or missing Linux Secret Service collection leaves a hook without a passphrase, it prints a one-line remedy to stderr and records the skip for `mm status`. Load-bearing warnings (corrupt manifests, missing sources, fsync or per-file apply failures) still print to stderr.

Add this to your global `~/.claude/CLAUDE.md`:

````markdown
# Mind Meld

At the **start of each conversation**, run:

```bash
mm autopull
```

- **No output.** Continue silently. Check `mm status` when you need to confirm the outcome.
- **Any output:** Surface conflicts, deletions, warnings and their suggested fixes.

At the **end of each conversation** (when wrapping up or completing the requested task), run:

```bash
mm autopush
```

- **No output:** Nothing was printed. Continue normally.
- **Any output:** Surface skips, warnings and their suggested fixes.

Skip these hooks when `mm` is not installed.
````

### How it works

- `mm autopull` pulls peer changes, writes `.mind-meld-log.md` into affected Claude projects, runs the managed Syncthing mesh pass, and starts a detached `qmd` memory-index refresh when enabled.
- `mm autopush` diffs enabled sources against the last push, uploads only changed blobs (holding files still being written), publishes `syncthing/<id>.enc` if needed, runs the managed mesh pass, and starts a detached memory-index refresh.
- Both run under the local lock, keep small capture budgets so hooks never stall a session, record `degraded` / `no-sources` breadcrumbs for `mm status` when something degrades, and run the once-a-day background self-update at their tail.

## Codex Integration

Enabling `codex` (`mm enable-source codex`) syncs `~/.codex/AGENTS.md`, `skills/`, and `plugins/` (excluding Codex's own `plugins/cache/*` and `plugins/.remote-plugin-install-staging/*`), enables Codex rollout usage capture (including archived sessions), maintains Codex's `/retro-fleet` skill link, and publishes this machine's Codex memory summaries (`memory_summary.md`, `MEMORY.md`, and direct `rollout_summaries/*.md` from `~/.codex/memories_v2/`, falling back to `~/.codex/memories/`).

Peers receive each publisher's Codex memory as read-only per-machine copies under `~/.local/share/mind-meld/codex-memory/<peer-device-id>/` — `mm` never writes into Codex's own memory folder. Credentials, session databases, and whole-file `config.toml` stay local. Symlinks inside any sync source are skipped on push and left untouched on pull.

Add the same lifecycle hook to `~/.codex/AGENTS.md`:

```markdown
# Mind Meld

At the start of each conversation, run `mm autopull`.
At the end of each completed task or conversation, run `mm autopush`.

If either command has output, summarize it for the user. If it is silent, continue silently.
```

### Searching agent memory (qmd)

Mind Meld can maintain one local [qmd](https://github.com/tobi/qmd) search index (`--index mind-meld`) over every enabled provider's memory so any agent on this machine can search what any other agent learned across your fleet:

- **Scope:** covers memory from projects present on this machine plus each agent's machine-wide memory (`mm-claude`, `mm-grok`, `mm-muse`, `mm-codex-<this-machine>`, and `mm-codex-<peer>` copies). Claude project memory for a project with no checkout on this machine stays off it and is not searchable here.
- **Local and derived:** markdown files stay the source of truth; the index lives locally (`~/.cache/qmd/mind-meld.sqlite`) and never syncs.
- **Refresh:** attended `mm push` and `mm pull` reconcile `mm-` collections and run `qmd --index mind-meld update` after releasing the sync lock; `autopush` and `autopull` refresh detached in the background. Previews never refresh. Failures never change a sync's exit code and surface in `mm status` / `mm diag`.
- **Embeddings (optional):** keyword search (`qmd --index mind-meld search`) downloads no models. Opt in during `mm init` (or set `embeddings = true` under `[memory_index]` in `config.toml`) to run `qmd --index mind-meld embed` in the background (~334 MB model on first run).
- **Commands:** `mm memory-index` (read-only status), `mm memory-index on` (requires `qmd` 2.8.3+), `mm memory-index off` (disables refresh and removes `mm-` collections).

#### Agent setup

1. **Add the pointer line** to your global instruction files (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, or `AGENTS.md` in your `CODEX_HOME`). For symlinked files, edit the target by hand:

   > If the owner mentions something they discussed with an agent before and you can't find it in your own memory, search every agent's memory with `qmd --index mind-meld search "<key words>" -n 10` (`qmd --index mind-meld get <docid>` reads a hit). If memories disagree, show the owner each one with its path and ask which is right.

2. **Claude Code allow rules:** add these to `~/.claude/settings.json` under `permissions.allow`:

   ```json
   "Bash(qmd --index mind-meld search:*)",
   "Bash(qmd --index mind-meld get:*)"
   ```

3. **Codex sandbox writable root:** `qmd` opens `mind-meld.sqlite` in WAL mode. Under `workspace-write`, add your qmd cache folder (`~/.cache/qmd`, or `$XDG_CACHE_HOME/qmd`) to `sandbox_workspace_write.writable_roots` in Codex's `config.toml`:

   ```toml
   [sandbox_workspace_write]
   writable_roots = ["/home/user/.cache/qmd"]
   ```

4. **Optional MCP setup (manual):** Mind Meld never edits agent MCP configs. Run these yourself if you prefer MCP access alongside or instead of the CLI:

   ```bash
   claude mcp add mind-meld -- qmd --index mind-meld mcp
   codex mcp add mind-meld -- qmd --index mind-meld mcp
   ```

## Other sources

### Grok Build

`mm enable-source grok` syncs `~/.grok` `skills/`, `commands/`, `rules/`, and `memory-v2/` topic Markdown (`memory-v2/global/topics/**/*.md` and `memory-v2/workspaces/*/topics/**/*.md`), and consents to reading completed turn totals from `~/.grok/sessions/*/*/usage.json` (including Paseo ACP runs). Sessions, SQLite indexes, `MEMORY.md`, `archive/`, `observations/`, `config.toml`, `auth.json`, `plugins/`, and `hooks/` stay local. Grok Build reads `~/.claude/skills`, so `mm` keeps no separate Grok skill link.

#### Grok memory across machines

Add `[memory]` with `enabled = true` to `~/.grok/config.toml` on each machine that should use Grok memory.

- **Topics only:** pending `observations/` (including `_inbox/`) stay local because Grok 1.0.77 does not consolidate transferred captures ([live check](docs/archive/host-parity.md#track-111a-review-follow-up-grok-1077-2026-11-06)). Before switching machines, run `/flush` then `/dream` in Grok, then `mm push`; run `mm pull` on the receiving machine before starting Grok.
- **Repository scopes:** global topics and repos with a network `origin` remote match across machines; no-origin repos and plain directories transfer as inert copies.

### Muse

`mm enable-source muse` syncs `~/.local/share/muse/memory/` (excluding `.muse-memory.lock`) and consents to reading completed `model_completed` step totals from local Muse session logs. Sessions, bundled skills, and `~/.config/muse` stay local. Muse discovers `~/.claude/skills`, so `mm` maintains no separate Muse skill link.

### Antigravity (agy)

`mm enable-source agy` syncs `~/.gemini/config` (`skills/`, `rules/`, `AGENTS.md`, `GEMINI.md`), maintains a `retro-fleet` skill link under `~/.gemini/config/skills/`, and consents to reading completed step token totals from `~/.gemini/antigravity-cli/conversations/<cid>.db`. Session databases, settings, hooks, and `agents/` stay local.

### Syncing gstack

When `~/.gstack` exists, `mm init` adds it as a whitelist-based `generic` source. Only cross-machine context syncs; per-run output and browser profiles (any directory containing a regular `Local State` file) stay local.

- **`include_dirs`:** `projects/`, `analytics/`, `retros/`
- **`include_files`:** `retro-context.md`, `greptile-history.md`, `.completeness-intro-seen`, `.telemetry-prompted`, `.proactive-prompted`, `.welcome-seen`, `.codex-desc-healed`
- **`include_patterns`:** everything under `analytics/` and `retros/`, the `include_files` above, and inside `projects/<slug>/` only cross-machine documents and ledgers (checkpoints, plans, designs, specs, briefs, cards, handoffs, deploy reports, pair-review reports, `learnings.jsonl`, `decisions*.jsonl`, `timeline.jsonl`, review logs). Full allowlist: [sync invariants](docs/invariants/sync.md#include_patterns-allowlist--inverse-consumer-filter).
- **`exclude_patterns` (machine-local state):** `config.yaml`, `projects/*/repo-mode.json`, `projects/*/land-deploy-confirmed`, `analytics/.last-sync-*`, `projects/*/decisions.active.json`, `projects/*/brain-cache/*`, `projects/*/pair-review/session.yaml`.
- **`exclude_patterns` (generated output):** `projects/*/venv/*`, `projects/*/.venv/*`, `projects/*/*.app/*`, `projects/*/.qa-evidence/*`, `*.zip`, `*.tar`, `*.tgz`, `*.tar.gz`, `*.tar.xz`, `*.tar.bz2`, `*.tar.zst`, `*.whl`, `*.so`, `*.dylib`, `*.pak`, `*.dmg`.

JSONL ledgers (`analytics/skill-usage.jsonl`, `projects/<slug>/timeline.jsonl`, etc.) set-union merge on pull. To customize `[[sync.sources]]` in `~/.config/mind-meld/config.toml`, copy the full default entry (supplying `sync.sources` replaces defaults), then run `mm migrate-config` to keep recommended excludes.

### Syncing gstack-extend

When `~/.gstack-extend/` exists, `mm init` adds it with `include_dirs = ["projects", "analytics/stage-runs"]` and `include_patterns = ["projects/**", "analytics/stage-runs/*.jsonl"]` so pair-review state, plans, and fleet stage-run history roam across machines.

### Nested git repositories

Walkers skip any directory strictly below a source root that contains a `.git` file or a `.git` directory with `HEAD` (except `codex-memory`, which reads only its hardcoded copy set inside Codex's Git-tracked memory dir). A folder published before it became a git checkout is frozen rather than deleted.

### Garbage collection

Attended content-changing pushes clean up this machine's orphaned blobs. If you use only hooks, run `mm gc` periodically to clean up orphaned blobs and expired local recovery copies. Use `mm gc --conflicts` to also reap redundant conflict copies older than 30 days.

## Disabling sources per machine

Run `mm disable-source <name>` to stop syncing a source on one machine without deleting local files or writing tombstones, or `mm enable-source <name>` to turn it back on. Use `mm reconfigure-sources` to revisit all source choices interactively.

## Managing agent skill links per machine

`mm` links `/retro-fleet` from the skill directories of authorized Claude Code, Codex, and Antigravity installs to the shared local copy at `~/.local/share/mind-meld/agent-skills/retro-fleet/`. Enabling a source authorizes its link. To authorize one without enabling sync, run `mm install-skills --agent <key>` or set `agents = ["claude", "codex"]` under `[skills]` in `config.toml`.

### Removing a skill link

To remove `/retro-fleet` from one agent while keeping `mm`, remove its symlink (for example `rm ~/.codex/skills/retro-fleet`). Routine pushes respect `removed-by-user`. Run `mm install-skills` to restore authorized links, or add `--agent <key>` to grant maintenance for that agent first.

## Fleet retro (`/retro-fleet`)

Run `/retro-fleet` in an agent or `mm retro-fleet 7d` (or `30d`, etc.) in a terminal to aggregate commits, active days, tokens, and estimated API list-rate cost across every machine in the fleet into one `## Agents` table (Claude, Codex, Grok, Muse, Antigravity).

**Cost legend:**
- `~` — estimate (may include a family-extrapolated rate named in `MM_HEALTH`).
- `≥` — floor of the priced subtotal (unpriced models, incomplete/catching-up/dropped readers, or Grok's unknown context tier; causes named in `MM_HEALTH`).
- `—` — unavailable, not zero.

**Rate provenance.** In `token_usage.py`: Anthropic `PRICING_LAST_UPDATED` = 2026-11-25 ([pricing](https://platform.claude.com/docs/en/about-claude/pricing)); OpenAI `PRICING_OPENAI_LAST_UPDATED` = 2026-11-25 ([Standard pricing](https://developers.openai.com/api/docs/pricing)); xAI `PRICING_XAI_LAST_UPDATED` = 2026-11-25 ([pricing](https://docs.x.ai/developers/pricing), [Build model identity](https://docs.x.ai/build/overview)); Google `PRICING_GOOGLE_LAST_UPDATED` = 2026-11-25 ([Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)); Muse `PRICING_MUSE_LAST_UPDATED` = 2026-11-25 ([pricing](https://dev.meta.ai/docs/pricing-rate-limits)). The CLI makes no network requests for prices. The shipped table uses the rate in effect on the date checked; scheduled vendor price changes (`gemini-4-carbon` and `gemini-4-argon` increase from `$2.00/$0.10/$0/$10.00` to `$4.00/$0.20/$0/$20.00` on 2026-12-01; `gemini-3.8-flash` increases from `$0.75/$0.075/$0/$3.75` to `$1.50/$0.15/$0/$7.50` on 2027-01-01; `gpt-5.6-sol` promotional pricing runs through 2026-11-30) take effect on the documented dates. When `/retro-fleet` checks vendor pricing before its second pass, it uses the rate active on the run's UTC date via `--set-model-price`.

**Pricing refresh and local overrides:** `mm retro-fleet` accepts `--set-model-price '<model>=<in>,<cache_read>,<cache_write>,<out>'` (USD per million tokens) and `--mark-pricing-verified`, storing local overrides in `~/.config/mind-meld/pricing-cache.json` (never synced; delete the file to reset to bundled rates).

**Git capture and `[retro] repo_roots`:** `mm push` discovers local git repositories automatically; add extra roots under `[retro] repo_roots = ["~/code"]` in `config.toml` if needed. Run `mm recapture 30d` to re-capture missed git commit history for a window (commits deduplicate fleet-wide on `(remote, sha)`).

## Handling conflicts

- **Fast-forward:** a peer's edit to a file you have not changed since your last sync can apply directly, without a recovery copy.
- **Auto-merge:** concurrent edits to `.jsonl` and `MEMORY.md` combine lines. In `.jsonl`, records with string `ts` sort by that value before the other lines.
- **Keep-both (default):** when two machines edit the same non-mergeable file, `mm pull` keeps the local version at its current path and saves the peer's version as `<stem>.sync-conflict-<YYYYMMDD-HHMMSS>-v1-<device>.<ext>`.
- **Resolve:** inspect with `mm conflicts`, resolve interactively with `mm resolve` (keep local, keep remote, merge, or promote an orphan sidecar), then `mm push`. Use `mm gc --conflicts` to clean up redundant conflict copies older than 30 days.

## Upgrading

```bash
mm update
```

`mm update` runs `pipx upgrade mind-meld` against the moving `@latest` branch (and re-pins any tag-pinned install back onto `@latest`). It needs no config or passphrase. Current attended git-root discovery gets a `1,500 ms` base budget (and `attended_capture_cap_ms` defaults to 30,000 ms).

For historical v0.x and v1.x per-release upgrade checklists, see [docs/archive/upgrading-1x.md](docs/archive/upgrading-1x.md).

### Automatic updates

Once per 24 hours, `mm pull`, `mm push`, `mm autopull`, and `mm autopush` check GitHub for a newer release after syncing. A `pipx` install tracking `@latest` upgrades itself automatically (foreground with spinner on attended `push`/`pull`; background to `~/.config/mind-meld/auto-update.log` on `autopush`/`autopull`). A failed update never changes the sync's exit code and surfaces in `mm status`.

To disable automatic updates or checks in `~/.config/mind-meld/config.toml`:

```toml
[upgrade]
auto_install = false   # keep check + notice, never run pipx
auto_check = false     # disable checks and updates completely
```

Run `mm --no-check-version <command>` to skip the check and update once. Before rolling back, check the [upgrade notes](docs/archive/upgrading-1x.md) and storage-format requirements for the target tag. On the Syncthing fleet, use a release that supports its shared folder layout and registrations. Force-reinstall the tag with `UV_VENV_CLEAR=1 pipx install --force git+https://github.com/kbitz/mind-meld.git@vX.Y.Z`. If you need the older bundled skill, move the local skill store aside before running `mm install-skills`. A pin holds until you run `mm update`.

## Versioning and compatibility

Mind Meld has one user and makes no compatibility promise beyond storage-format gates: an older `mm` refuses storage written in a format it cannot read and tells you to upgrade. See [docs/SPEC.md](docs/SPEC.md#release-policy).

## Troubleshooting

<a id="history-database"></a>
### History database (state.db)

`~/.config/mind-meld/state.db` holds local history (`mm log`), the pull ledger, and peer cursors. Run `mm diag` to tell an interrupted write from damage. If the database is busy, wait for the other command. For a newer schema, run `mm update`. After an interrupted write, let the next attended `mm push` or `mm pull` recover its journal.

If the database is damaged, stop sync commands and hooks. Move `state.db` and its `state.db-*` companions **together** to a backup directory. The next sync builds a fresh database. Keep the backup if you need the previous history.

### Recovering a deleted file

When a peer deletes a file and your copy is byte-identical to the version it deleted, `mm pull` moves your copy to `~/.config/mind-meld/deleted/<UTC-stamp>-<rand>/<source>/<path>` for 30 days instead of unlinking it. Find past deletions with `mm log --action deleted`.

Restore the file **on the machine that deleted it** (a copy restored only on a follower stays suppressed by the origin's deletion record). Transfer the recovery folder to the origin, set the paths for your file (`cp -n` never overwrites a newer file), and push:

```bash
copies="$HOME/mm-recovery-20261101-181530-3fa9"  # the transferred recovery folder
source_name=claude                              # mm sources: Name
source_dir="$HOME/.claude"                     # mm sources: Path on the origin
copy_path="projects/-home-user-myapp/memory/user_role.md"    # inside the follower's copy
origin_path="projects/-Users-user-myapp/memory/user_role.md" # inside the origin's source
mkdir -p "$(dirname "$source_dir/$origin_path")"
cp -n "$copies/$source_name/$copy_path" "$source_dir/$origin_path"
mm push
```

**Many files were deleted by mistake:** Pause the `mm autopull` hook on machines that have yet to pull. Use `mm pull --dry-run` to see which files are queued for deletion. Gather recovery copies from machines that already pulled, restore them on the origin as above, and push there. Resume hooks and attended pulls once that push has been delivered.

### Syncthing stalled or needs a restart

If Syncthing stops delivering files or appears stuck, restart it cleanly from the CLI and recheck fleet state:

```bash
syncthing cli operations restart
mm status
mm diag
```

### Another mm operation is running

`mm` holds an exclusive lock at `~/.config/mind-meld/mind-meld.lock` while syncing. Wait for the other command or hook to finish and retry. Never delete a live lockfile.

### Auto commands skip: keyring locked or unavailable

On Linux, `autopull` and `autopush` never trigger a desktop keyring unlock prompt. Unlock your login keyring in the desktop session (or create a default collection in Secret Service and store the passphrase) so hooks can read the passphrase. On macOS, run `mm push` or `mm pull` in a terminal in your desktop session and choose **Always Allow** in the Keychain dialog.

### Retiring a dead machine

1. Pull on a machine with the same home folder as the one you're retiring to bring its available project memory onto disk. Keep that device's manifests and blobs for any work still to pull.
2. Shut down the retired machine or stop its `mm` hooks and Syncthing. A later push from that machine can register it again.
3. Run `mm devices retire <name|id>` on an active machine. Interactive runs confirm with Enter defaulting to no. For `--yes` or non-interactive runs, use the exact 8-character ID from `mm devices`.

Managed peers wait for the device to disconnect. On owner-managed peers, remove it in the Syncthing GUI, including at the introducer if it can reintroduce the retired device.

### Newer or damaged storage format

If `mm` reports a newer format, leave the key headers in place and run `mm update`. Verify with `mm --version` and retry. If the latest release still reports damage, use `mm diag` to inspect key-header state. For a damaged peer manifest, use the publishing machine named in the `mm pull` warning and let `mm push` try its normal recovery there.

If diagnostics identify a missing or unreadable key header, wait for Syncthing to finish. Get a known-good `mm-crypto-init` from a machine that can still sync **this same fleet folder** or from a verified backup of that fleet. Back up every header outside the shared folder. Move any damaged canonical file aside, then restore the verified copy to `mm-crypto-init` without overwriting another file. Check with `mm pull` and keep the backups. If you cannot verify a copy belongs to this fleet, stop before changing its storage.

### Key header copies differ

`mm` removes only byte-identical copies after verifying the passphrase. It preserves differing copies, which may contain the only usable header. A pending-copy count in `mm status` or `mm diag` does not by itself need repair.

If a fingerprint mismatch persists after Syncthing finishes, use a machine that previously synced successfully. Back up **every** header outside the shared folder, then move extra copies to the backup one at a time, keeping a usable header in place. Run `mm status` after each move. Stop when the mismatch clears. If a move causes a missing-header error, undo it immediately. If no move helps, restore all copies. When the canonical file is missing or unreadable, use the [restore procedure above](#newer-or-damaged-storage-format). Never reinitialize or re-key existing storage.

### Waiting content is not a failure

If a peer's manifest arrived before some of its encrypted blobs finished syncing over Syncthing, `mm pull` applies everything that has arrived and reports what is still waiting. Run `mm pull` again once Syncthing finishes delivering.

### Pull incomplete / could not pull a file

When a file fails to apply, `mm pull` applies the remaining healthy files, prints one `mm: warning:` line per failure, and exits 1. Fix the reported problem and rerun `mm pull`. A corrupt peer manifest produces a warning and skips that peer while pulls from other peers continue.

For full machine-readable triage (no passphrase or valid config required), run `mm diag --json`. Top-level keys: `mm_version`, `config`, `crypto_init`, `keyring`, `root_salt_drift`, `sidecar`, `storage_inventory`, `project_index`, `sync_scope`, `last_autorun`, `last_attended_runs`, `sync_run_projection`, `skill_links`, `host_usage`, `host_read_budgets`, `host_publication`, `discovery`, `git_capture`, `fast_forward`, `syncthing`, `memory_index`, `capture_catchup`, `push_timings`. Each `skill_links` row includes `key`, `agent`, `target`, `store`, `store_state`, `status`, and `maintain_links`.

### Dropped repositories and ignored git environment variables

`mm` scrubs repository-local `GIT_*` environment variables when running git walks. During `mm recapture`, repository discovery gets 30 s, each repository gets 30 s, and the whole walk gets 120 s (larger than the normal attended push caps). If a repository still fails or times out, inspect `mm diag` under `discovery` and `git_capture`.

<a id="host-usage-capture"></a>
### Host usage capture (Codex, Grok Build, Muse and Antigravity)

Every attended `mm push` refreshes and publishes consented host usage, even when there are no Git roots or content changes. **Exit 0 means content sync succeeded regardless of capture outcome** — verify recorded capture and `Publication: published` with `mm status` and `mm diag`.

Capture requires enabled `mm-events` and consented readers (`mm enable-source codex|grok|muse|agy`). Attended `mm push` shares `[retro] capture_budget_ms` (default 5,000 ms) and `[retro] attended_capture_cap_ms` (default 30,000 ms, up to 60,000 ms) across discovery, git walks, and host readers; unfinished work resumes on later pushes or via detached background catch-up (`autopush`).

Healthy:

<!-- usage-example:healthy -->
```text
Host usage last recorded capture: 2026-09-21T12:00:00+00:00 (0d ago)
  Publication: published (accepted manifest evidence)
  codex: contributed
  Antigravity: contributed
  Last recorded attended attempt: 2026-09-21T12:00:00+00:00 (0 s ago) — published
```

Mixed with an empty reader:

<!-- usage-example:mixed-empty -->
```text
Host usage last recorded capture: 2026-09-21T12:00:00+00:00 (0d ago)
  Publication: published (accepted manifest evidence)
  codex: contributed
  Antigravity: completed, no usage in snapshot
  Last recorded attended attempt: 2026-09-21T12:00:00+00:00 (0 s ago) — published
    codex contributed; agy completed, no usage in snapshot
```

Failed refresh (earlier capture remains visible; inspect named readers in `mm diag`):

<!-- usage-example:failed-refresh -->
```text
Host usage last recorded capture: 2026-09-21T11:00:00+00:00 (0d ago)
  Publication: published (accepted manifest evidence)
  codex: contributed
  Antigravity: contributed
  Last recorded attended attempt: 2026-09-21T12:00:00+00:00 (0 s ago) — no-row
    codex dropped (unsupported); agy dropped (deadline)
```

Unverified (a line over 16 MiB prevents proof from that file; check `mm status` read-only):

<!-- usage-example:unverified -->
```text
Host usage last recorded capture: unknown (~/.local/share/mind-meld/events/local-2026-09-21.jsonl)
  Publication: unverified (oversized-line) (accepted manifest evidence)
  codex: contributed
  Antigravity: contributed
  Last recorded attended attempt: 2026-09-21T12:00:00+00:00 (0 s ago) — unverified (oversized-line)
  Usage refresh: Attended mm push refreshes and publishes host usage automatically.
```

#### Reader blockers

`mm diag` separates `<reader> cache inventory:` from `<reader> usage read blocker:`. If a host updates its session format to one this `mm` cannot parse, that reader alone fails closed while other readers keep publishing: run `mm update` (or `mm disable-source <name>` to stop retrying).

### Snapshot failures

If a file changes, vanishes, or grows while `mm push` scans or uploads it, `mm` can hold that file at its last published version and publish the rest. A failed or incomplete scan of an enabled user source refuses the push rather than guessing which files were deleted. The default local event history has its own [deletion rules](#uninstalling).

### Shell completion

```bash
mm --install-completion
```

## Uninstalling

Only want `/retro-fleet` removed from one agent? See [Removing a skill link](#removing-a-skill-link).

`pipx uninstall mind-meld` removes the command. Leave the local data and config in place if you plan to reinstall. Removing only `~/.local/share/mind-meld` while keeping your config can make the next push publish missing default event files as deletions.

For a full cleanup, first save any authored skills from the local skill store and any recovery copies you still need from `~/.config/mind-meld/deleted/`. These commands leave the shared Syncthing folder in place for the rest of your fleet.

```bash
pipx uninstall mind-meld

# Remove only store-backed Mind Meld skill links (leaves your own files untouched):
for l in ~/.claude/skills/retro-fleet ~/.codex/skills/retro-fleet ~/.gemini/config/skills/retro-fleet ~/.config/opencode/skills/retro-fleet; do
  [ "$(readlink "$l")" = "$HOME/.local/share/mind-meld/agent-skills/retro-fleet" ] && rm -f "$l"
done

# Local skill store and mm-events log:
rm -rf ~/.local/share/mind-meld

# Config, device identity, caches, pull ledger (state.db), and 30-day deleted-file copies:
rm -rf ~/.config/mind-meld

# Keychain entry (macOS):
command -v security >/dev/null && security delete-generic-password -s mind-meld -a passphrase

# Secret Service entry (Linux):
command -v secret-tool >/dev/null && secret-tool clear service mind-meld username passphrase
```

## Development

CI skips draft PRs and runs three jobs on ready PRs and `main`:
- `ci` (`macos-latest`, Python 3.13): `./bin/check --no-bootstrap`, real Keychain assertion, isolated wheel smoke.
- `portable-linux` (`ubuntu-latest`, Python 3.14): `./bin/check --no-bootstrap`, wheel smoke.
- `secret-service-live` (`archlinux:latest`): live `gnome-keyring` test as an unprivileged user.

Run portable checks locally from any clone (bootstraps `.venv` if needed, runs `ruff check .`, `ruff format --check .`, and pytest):

```bash
./bin/check
./bin/check --affected            # coverage-selected tests for your branch
./bin/check tests/test_config.py  # scoped pytest (still lints whole repo)
```

Flags: `--tests` (skip lint), `--lint` (skip pytest), `--serial` (disable xdist), `MM_PYTEST_WORKERS=N`, `MM_LIVE_SECRET_SERVICE=1 ./bin/check --tests tests/test_secret_service_live.py` (Linux live keyring). See `./bin/check --help`.

## Architecture

See [SPEC.md](SPEC.md) for the data model and wire architecture, and [docs/SPEC.md](docs/SPEC.md) for product scope and release policy.
