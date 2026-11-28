# Mind Meld

Mind Meld (`mm`) keeps your AI coding agents' memory, skills and instructions the same on every machine you code on, and reports what those agents did across all of them. Finish on one machine and run `mm push`. Start on another, run `mm pull`, and the context is there.

## Is it safe for my setup?

`mm` encrypts your synced files and file lists with AES-256-GCM before they reach the shared folder. The key header and device registry are readable, including machine names, `mm` versions and last-seen times. `mm` syncs an allowlist: agent memory, skills, rules, instruction files and gstack project state. Session transcripts, session databases, credentials and whole-file settings stay on the machine, and <a id="nested-git-repositories"></a>git checkouts nested inside a synced folder are skipped. It runs on macOS and Arch-based Linux, moves files through one [Syncthing](https://syncthing.net/) folder and has no server. It is built for one person's machines.

## Quick start

On macOS, install [Homebrew](https://brew.sh/) first.

1. Install `mm`. The script adds Python 3.11+, `pipx` and Syncthing 2.1+ where they are missing (plus `gnome-keyring` and `libsecret` on Arch), and tries to install `qmd`:

   ```bash
   mm_bootstrap=$(curl -fsSL https://raw.githubusercontent.com/kbitz/mind-meld/latest/bin/bootstrap) && sh -c "$mm_bootstrap"
   ```

2. Open Claude Code once if you want to sync its project memory. On Arch, unlock your login keyring in the desktop session. Start a new fleet on your first machine, choose a fleet passphrase and enable at least one source when asked. `mm` sets up Syncthing and prints this machine's Syncthing device ID.

   ```bash
   mm init --new-fleet
   ```

3. Publish this machine's context:

   ```bash
   mm push
   ```

Then [add your other machines](#add-a-machine). If you already run Syncthing with your own devices or folders, the script and `mm init` leave it alone; follow [Configuring Syncthing yourself](#configuring-syncthing-yourself). For a manual install, have Git, Python 3.11+, `pipx` and Syncthing 2.1+ ready, plus `gnome-keyring` and `libsecret` on Arch. Run `pipx install git+https://github.com/kbitz/mind-meld.git@latest`.

## Features

- <a id="syncing-gstack"></a>**Agent context sync** for Claude Code, Codex, Grok Build, Muse, Antigravity, gstack and gstack-extend. [docs/SPEC.md](docs/SPEC.md#what-mm-syncs-and-reports) lists what each one syncs.
- **Project memory follows the repository.** Claude Code project memory matches across machines by git `origin`, so `/Users/user/code/app` and `/home/user/src/app` share one memory.
- **Conflicts kept both ways.** Another machine's edit to a file you haven't changed applies directly, and `.jsonl` files and `MEMORY.md` merge line by line. Any other clash keeps your copy and saves the peer's beside it as a `.sync-conflict-*` file until you run `mm resolve`.
- **30-day recovery.** When another machine deletes a file, `mm pull` moves your unchanged copy to `~/.config/mind-meld/deleted/` for 30 days.
- **Fleet usage reports.** `/retro-fleet` in an agent, or `mm retro-fleet 7d` in a terminal, totals commits, active days, tokens and estimated API cost across every machine.
- **Cross-model memory search.** A local `qmd` index lets any agent search what every agent on every machine has learned.
- **Hooks.** `mm autopull` and `mm autopush` run from agent instructions, never prompt and print nothing when all is well.
- **Self-update.** `mm update` installs the newest release. Push, pull and the hooks also do it on their own, at most once a day.

## Add a machine

Install with the [bootstrap command](#quick-start) and repeat the agent and keyring setup from the first machine. Then run:

```bash
mm init
```

Choose join (`j`). `mm init` asks for the storage path, the Syncthing device ID of one machine already in the fleet, a name for this machine and the sources to sync. It saves those answers (never the passphrase) and prints an approval command. Find a fleet machine's device ID under **Actions → Show ID** in its Syncthing GUI (`http://127.0.0.1:8384`).

On that fleet machine, check that the 7-character short ID matches, then approve:

```bash
mm devices approve <short-id>
```

The new machine waits up to 10 minutes. Once approved, it asks for the fleet passphrase and pulls. Run `mm push` on it when `mm init` finishes. Other machines pair with it on their next push or pull. If the wait runs out, run `mm init` again and resume with your saved answers.

### Configuring Syncthing yourself

`mm init` sets up Syncthing only when its configuration is still the default. If Syncthing already has paired devices, custom folders or a non-empty Default Folder, set it up by hand:

1. In the Syncthing GUI, add one fleet machine with **Add Remote Device**. On the joining machine, mark that fleet machine as an **Introducer** so it learns about later machines.
2. On the fleet machine, share the folder with ID `mind-meld` with the new machine. On the new machine, accept it at `~/Sync/mind-meld` (or your storage path).
3. Wait for the `mm-crypto-init` file to arrive, then run `mm init`, choose join and enter the fleet passphrase.

To approve a join from a fleet machine where you manage Syncthing, accept the new device in its GUI, then tick it on the `mind-meld` folder's **Edit → Sharing** tab.

<a id="syncthing-folder-options"></a>Folder settings, which `mm status` and `mm diag` check:

- Folder type Send & Receive, file versioning off and pull order alphabetic.
- **Ignore permissions** off, and **Maximum conflicts** `-1` or a positive number.
- `(?d)tmp*.tmp` in the folder's `.stignore`.
- If you also share the parent `~/Sync`, add `/mind-meld` to the parent's `.stignore`.

## Retire a machine

<a id="retiring-a-dead-machine"></a>

1. Run `mm pull` on a machine with the same home folder path. Check that the retiring machine's last project memory and any other files you still need have arrived before retiring it. Leave its manifests and blobs in the shared folder.
2. Shut the old machine down, or stop its hooks and Syncthing. A later push from it registers it again.
3. On an active machine, retire it by name, or by the 8-character ID that `mm devices` lists:

   ```bash
   mm devices retire <name|id>
   ```

Machines where `mm` manages Syncthing drop the device once it disconnects. Where you manage Syncthing yourself, remove the device in the GUI, including on any introducer.

## Everyday commands

<a id="sync-outcomes"></a><a id="status-verdicts"></a><a id="previews"></a>

```bash
mm pull             # bring in other machines' changes
mm push             # publish this machine's changes
mm status           # one-line verdict, then Syncthing and per-machine state
mm diag             # non-secret diagnostics; runs without a passphrase or config
mm pull --dry-run   # preview: changes nothing except the lock file
mm conflicts        # list conflict copies; mm resolve picks a winner for each
```

Each command prints one line per thing that happened.

```text
$ mm push
Push: 10 content changes published; usage published.

$ mm pull
Pull: 1 content change from mac-a.
```

Errors start with `mm: error:` and end with the command that fixes them (`Run: mm update`). `mm push --dry-run` previews a push. `mm --help` lists every command, and [SPEC.md](SPEC.md#commands) has each flag.

## Agent setup

### Claude Code integration

Add this to `~/.claude/CLAUDE.md` so every session pulls first and pushes last:

````markdown
# Mind Meld

At the **start of each conversation**, run:

```bash
mm autopull
```

At the **end of each conversation**, run:

```bash
mm autopush
```

If either command prints anything, surface it to the user. Skip these hooks when `mm` is not installed.
````

For Codex, add the same two steps to `~/.codex/AGENTS.md`.

### Memory search

`mm init` offers the index. To turn it on later, run `mm memory-index on` (it needs `qmd` 2.8.3+). Then:

1. Add this line to each agent's global instructions (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`). If the file is a symlink, edit its target.

   > If the owner mentions something they discussed with an agent before and you can't find it in your own memory, search every agent's memory with `qmd --index mind-meld search "<key words>" -n 10` (`qmd --index mind-meld get <docid>` reads a hit). If memories disagree, show the owner each one with its path and ask which is right.

2. Let Claude Code run those commands by adding these to `permissions.allow` in `~/.claude/settings.json`:

   ```json
   "Bash(qmd --index mind-meld search:*)",
   "Bash(qmd --index mind-meld get:*)"
   ```

3. Codex in `workspace-write` mode needs the `qmd` cache to be writable. Add it to `~/.codex/config.toml`:

   ```toml
   [sandbox_workspace_write]
   writable_roots = ["/home/user/.cache/qmd"]
   ```

4. Optional MCP access, which `mm` never configures for you:

   ```bash
   claude mcp add mind-meld -- qmd --index mind-meld mcp
   codex mcp add mind-meld -- qmd --index mind-meld mcp
   ```

The index lives at `~/.cache/qmd/mind-meld.sqlite`, refreshes after each push and pull, and never syncs. `mm memory-index off` stops it and removes its collections.

<a id="automatic-updates"></a>

## Upgrading

```bash
mm update
```

On a `pipx` install from the `latest` branch, which the bootstrap sets up, push, pull and the hooks check for a new release once a day and install it. A failed update never changes the sync's result. To turn that off, set it in `~/.config/mind-meld/config.toml`:

```toml
[upgrade]
auto_install = false   # still tell me about new releases
auto_check = false     # never check
```

To go back to an older release, run `UV_VENV_CLEAR=1 pipx install --force git+https://github.com/kbitz/mind-meld.git@vX.Y.Z`. It stays pinned until you run `mm update`.

## Troubleshooting

### Syncthing stalled

If files stop arriving, restart Syncthing and check again:

```bash
syncthing cli operations restart
mm status
```

### Another mm operation is running

`mm` holds a lock at `~/.config/mind-meld/mind-meld.lock` while it syncs. Wait for the other command or hook to finish, then retry. Never delete a live lock file.

### Auto commands skip: keyring locked or unavailable

The hooks never open an unlock prompt. If they skip, run `mm status` for the cause. On Linux, unlock the login keyring in your desktop session. If Secret Service has no default collection, use its desktop prompt to create one and save the fleet passphrase:

```bash
secret-tool store --label=mind-meld service mind-meld username passphrase application 'Python keyring library'
```

On macOS, run `mm pull` in your desktop terminal and choose **Always Allow** in the Keychain dialog.

### Pull incomplete / could not pull a file

`mm pull` applies every healthy file, prints one warning per failure and exits 1. Fix what the warning names and run `mm pull` again. If content is still on its way, wait for Syncthing to finish and pull again. For full detail, run `mm diag --json`.

### Snapshot failures

A file that changes while `mm push` reads it keeps its last published version, and the rest publishes. If `mm` can't finish scanning a source, it refuses the push rather than guess what was deleted. Fix the reported file and push again.

### Recovering a deleted file

Find past deletions with `mm log --action deleted`. Restore the file **on the machine that deleted it**; a copy restored only on another machine stays deleted. Copy the recovery folder to that machine, set the paths for your file, and push:

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

If many files were deleted by mistake, pause pull hooks on unaffected machines. Restore and push on the origin, then wait for Syncthing to deliver the new manifest. Resume pulls and hooks once `mm pull --dry-run --verbose` on each peer shows that it would leave the restored files in place. Save recovery copies you still need before `mm gc` or a later pull clears copies older than 30 days.

<a id="history-database"></a>
### History database (state.db)

`~/.config/mind-meld/state.db` holds local history and the pull ledger. Run `mm diag` to tell an interrupted write from damage; the next `mm push` or `mm pull` recovers an interrupted write. If it is damaged, stop sync commands and hooks, then move `state.db` and its `state.db-*` files together to a backup folder. The next sync builds a fresh one.

### Newer or damaged storage format

If `mm` reports a newer format, leave the key header in place and run `mm update`. If the latest release still reports damage, run `mm diag` and wait for Syncthing to finish. If `mm-crypto-init` is missing, unreadable or confirmed to be from the wrong fleet, restore only from a verified copy of this fleet's header. Back up every header outside the shared folder before moving a damaged or wrong canonical file aside. Copy the verified header into `mm-crypto-init` without overwriting another file. Keep any usable conflict copy in place until the canonical header is restored. Verify with `mm pull`. Never initialize a new fleet or re-key this folder to repair it.

### Key header copies differ

`mm` removes only identical copies of the key header. A count of differing copies alone needs no action. If a fingerprint mismatch persists after Syncthing finishes, use a machine that previously synced successfully and back up every header outside the shared folder. Keep a usable header in place. Move extra copies aside one at a time and check `mm status` after each move. Stop when the mismatch clears. Undo a move immediately if the header goes missing, and restore all copies if none of the moves helps. If the canonical header itself is wrong, use the [restore steps above](#newer-or-damaged-storage-format) with a copy that matches the fleet's expected fingerprint. Stop if you can't verify a copy.

### Dropped repositories and ignored git environment variables

If commits are missing from `/retro-fleet`, run `mm recapture 30d`. Repositories that still fail are named under `discovery` and `git_capture` in `mm diag`.

<a id="host-usage-capture"></a>
### Host usage capture (Codex, Grok Build, Muse and Antigravity)

Every attended `mm push` refreshes and publishes consented host usage, even when there are no Git roots or content changes. Exit 0 means content sync succeeded regardless of capture outcome. Check both `Publication: published` and the last attended attempt in `mm status` to see if this push refreshed usage. A reader that can't parse a new session format stops alone; run `mm update`, or `mm disable-source <name>` to stop it.

## Uninstall

Run `pipx uninstall mind-meld`. Keep the local data and configuration together if you might reinstall.

**Removing a skill link:** to drop `/retro-fleet` from one agent only, delete its symlink (for example `rm ~/.codex/skills/retro-fleet`). `mm install-skills` puts it back.

For a full cleanup, first save any skills you wrote in `~/.local/share/mind-meld/agent-skills/` and any recovery copies you need from `~/.config/mind-meld/deleted/`. The shared Syncthing folder stays in place for your other machines.

```bash
pipx uninstall mind-meld
for l in ~/.claude/skills/retro-fleet ~/.codex/skills/retro-fleet ~/.gemini/config/skills/retro-fleet ~/.config/opencode/skills/retro-fleet; do
  [ "$(readlink "$l")" = "$HOME/.local/share/mind-meld/agent-skills/retro-fleet" ] && rm -f "$l"
done
rm -rf ~/.local/share/mind-meld ~/.config/mind-meld
command -v security >/dev/null && security delete-generic-password -s mind-meld -a passphrase   # macOS
command -v secret-tool >/dev/null && secret-tool clear service mind-meld username passphrase     # Linux
```

## More detail

- [docs/SPEC.md](docs/SPEC.md): what `mm` is for, what each agent syncs, known limits and release policy.
- [SPEC.md](SPEC.md): architecture, data model and the [command reference](SPEC.md#commands).
- [docs/invariants/README.md](docs/invariants/README.md): module map and the rules behind each part.
- [docs/archive/upgrading-1x.md](docs/archive/upgrading-1x.md): historical upgrade notes for 0.x and 1.x.
- [CHANGELOG.md](CHANGELOG.md): what changed in each release.

## Development

Run `./bin/check` for lint and the full test suite, or `./bin/check --affected` for the tests your change reaches. CI runs the same checks on macOS and Linux, plus a live Linux keyring test.

[![CI](https://github.com/kbitz/mind-meld/actions/workflows/ci.yml/badge.svg)](https://github.com/kbitz/mind-meld/actions/workflows/ci.yml)
