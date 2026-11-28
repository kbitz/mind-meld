# Historical 0.x and 1.x Upgrade Notes

> **Archived (historical):** Per-release upgrade checklists and migration notes from
> Mind Meld 0.x and 1.x, relocated from `README.md` during the 2.0 documentation pass.
> For current upgrade and automatic update behavior, see [README.md](../../README.md#upgrading).


**Dedicated `mind-meld` qmd index (Track 163A).** Mind Meld now keeps its
`mm-` collections in a dedicated `mind-meld` qmd index (`--index mind-meld`)
instead of qmd's default index. After upgrading, run `mm memory-index on` or
let an enabled sync refresh succeed on each machine that uses the index.
This builds the dedicated index. On machines with a legacy refresh record,
it also removes the old `mm-` collections from the default index. Then use
[Agent setup](../../README.md#agent-setup) to replace the old pointer line. Complete the
refreshes before editing shared instruction files. Turning the index off
before migration can clear the record and leave legacy collections in the
default index.

**Codex memory folded into `codex` (Track 161A).** Codex memory copies now
sync with the `codex` source. Run `mm migrate-config` on each machine with
an old `codex-memory` source entry or name in `disabled_sources` to remove it.
The `codex` source controls selection as soon as you upgrade. If you had
disabled only `codex-memory`, the machine resumes publishing memory while
`codex` is enabled. If you had selected only `codex-memory`, run
`mm enable-source codex` to resume exchanging copies. This also enables Codex
customization sync and usage capture.

`mm enable-source codex-memory` and `mm disable-source codex-memory` print
a notice with `mm enable-source codex` or `mm disable-source codex`,
respectively, and leave your configuration intact.

Memory now uses the `codex` source's path. If the resolved root changes,
peers keep their copies of files found only at the previous root. A push
from the new root records it in the manifest even when the files are identical.
Legacy memory file exclusions apply immediately after upgrade.
`mm migrate-config` and `mm reconfigure-sources` preserve them in
`sync.codex_memory_exclude_patterns`, a list of globs relative to the Codex
source path.
Peers on 1.12.76–1.12.79 with `codex-memory` enabled keep exchanging copies.

**Automatic Syncthing pairing (Track 152A).** The first `mm push` after upgrading
publishes this machine's encrypted Syncthing record when Syncthing is running.
Run `mm update` and then `mm push` on each existing Syncthing fleet machine.
See [Syncthing folder options](../../README.md#syncthing-folder-options) for the one-approval join steps.

**Plain `mm init` prompt order, approval wait, and saved join answers (Tracks 151A, 154A):**
plain `mm init` asks `Start a new fleet or join an existing one? [n/j]`.
Answer `n`/`new` or `j`/`join`. Pressing Enter asks again, and
`mm init --new-fleet` skips the question. Before waiting, a fresh join asks
for the storage path, the fleet machine's Syncthing device ID if mm needs to
pair, the device name, the sources and the applicable
[memory-search setup questions](../../README.md#searching-agent-memory-qmd). Every yes/no
question uses `[y/n] (Enter = y)` or `[y/n] (Enter = n)`, and every source
defaults to yes when detected on disk. It saves those answers locally in
`~/.config/mind-meld/join.toml` (never the passphrase). When a managed join
needs approval, the same run prints both the short-ID and full-ID `mm devices approve` commands and waits for up to about 10 minutes plus local
Syncthing request time. A stopped join offers to resume from the saved answers
with `Continue joining? [y/n] (Enter = y)`.
Scripts and tests that supply input for a fresh join must send `j` or `join`
before the storage path, then supply the device name, source answers and any
memory-search setup answers before the passphrase.

**Fleet approval and retirement (Track 154A).** Update the approving machine to use
a name or short ID at a terminal. For an older release or a script, use the full
Syncthing ID printed by the joining machine. Init offers Grok Build and Muse and
defaults to yes when it detects their supported files or directories. On each
managed machine, a push or pull restores the Mind Meld name in Syncthing for this
machine and configured peers that a fleet record vouches for.
Update managed peers so they apply new [retirements](../../README.md#retiring-a-dead-machine).

**`mm retro-fleet --no-save` removed (Track 145A):** `mm retro-fleet --no-save` is
removed and now exits 2 as an unknown option (`mm: error: No such option: --no-save`).
Remove `--no-save` from any scripts or aliases calling `mm retro-fleet`. If an agent
session still holds a stale `retro-fleet` skill copy that passes `--no-save`, run
`mm install-skills` and then restart the agent so it reloads `SKILL.md`.

**Migration, gc and source warning output (Track 137A):** migration, backfill,
init-cleanup and forced unknown-source warnings now use the shared stderr
forms, with fix commands last when present. The malformed-blob summary from
`mm gc` and attended content-changing pushes moves from stdout to stderr;
verbose/dry-run per-key listings stay on stdout. Source outcomes name the
verb once (`Disable: source 'gstack' disabled on this device.`).
Update scripts matching the old wording or stream. No migration is needed;
exit codes, prompts and stored data are unchanged.

**Cursor support removed (Tracks 114A–116A):** `mm cursor-agent`,
`mm capture-cursor-usage`, the `cursor` source alias for `mm enable-source` /
`mm disable-source`, the Cursor usage reader, the Cursor git-root discovery prober, and
Cursor pricing provenance are removed. Scripts and aliases that ran `mm cursor-agent ...`
must call `cursor-agent ...` directly; the old command now exits 2 without starting
Cursor. On any machine where `mm enable-source cursor` was run earlier, delete the `stop`
hook entry whose command is `mm capture-cursor-usage` from `~/.cursor/hooks.json` and keep
your other hooks; until you do, Cursor runs that missing command after every turn.
Leftover private files under `~/.config/mind-meld/` (`cursor-host-tokens.json`,
`cursor-host-tokens.json.lock`, `cursor-standalone-spool.jsonl`,
`cursor-standalone-spool.jsonl.merging`) and any `[retro] cursor_host_usage` key in
`~/.config/mind-meld/config.toml` are ignored and can be deleted; git-root discovery drops
any cached `cursor` prober state on its next complete pass. Repositories that only
Cursor's records named leave discovery from the first push after upgrading and are no
longer captured: on the Mac that works in them, pin any you still use in
`[retro] repo_roots`, then run `mm recapture 90d` there once to collect the commits it
missed (`retro-fleet` counts each commit once, so the full window is safe). Older
published `host-usage-snapshot` rows that name `cursor` remain unchanged, with no
migration: `mm retro-fleet` labels a failed or incomplete Cursor read on those rows
`unknown/retired reader`, and counts retained `grok-4.7-fast` or effort-suffixed tokens in
the Grok row as unpriced while `grok-4.7` and `grok-4.7-build` stay priced from xAI's
rates. A machine with another consented host reader replaces its earlier snapshot on the
next push that captures host usage, while a machine with no consented host readers skips
pre-push attended host capture on a converged `mm push` and replaces its earlier snapshot
with an empty `host-usage-snapshot` (`token_sources: []`) on its next push with
content/identity changes or `mm autopush` (or until its earlier row ages past the 90-day
retention window). A last attended-attempt record written by mm 1.12.21 or older that
names Cursor shows `record corrupt — the next attended mm push rewrites it` until that
push.

**Grok Build `memory-v2/` sync (Track 111A):** on machines where the `grok` source is
already enabled, the first `mm push` after upgrading publishes existing topic
Markdown under `~/.grok/memory-v2/global/topics/**/*.md` and
`~/.grok/memory-v2/workspaces/*/topics/**/*.md`, without another prompt. Run
`mm update`, then `mm push` to publish them now. `skills/`, `commands/` and
`rules/` continue syncing. All `observations/` files, including `_inbox/`, remain
local, as do `MEMORY.md`, SQLite databases and their `-shm`/`-wal` files,
`archive/` and `.retired-stores/`. Before switching machines, run `/flush` then
`/dream` in Grok, then `mm push` in your shell; only the resulting topics travel.

No config migration is required. The first publishing push records the
`memory-v2/.mind-meld-anchor` without minting topic deletions from a pre-upgrade
manifest. Older mm versions may receive topic files and valid deletions, but do
not publish topics. Use `mm devices` to find older machines and run `mm update`
there. If populated stores have different bytes at the same topic path, pull
keeps the local file and writes a conflict copy; use `mm conflicts` and
`mm resolve` before starting the next Grok session. To keep customizations and
usage capture while leaving memory local, add `"memory-v2/*"` to the `grok`
source's `exclude_patterns`; to stop
Grok sync and usage capture, run `mm disable-source grok`. See
[Grok Build](../../README.md#grok-build) for the scope, configuration and handoff walkthrough.

**Git-root discovery finishes across attended pushes (Track 106A):** a cold Codex
index used to need more pushes than discovery's 100 ms attended budget could
give, so the retro cursor stayed held. An attended `mm push` now gives discovery
1,500 ms plus the attended scan-cap extension, clamped to the shared tail deadline; autopush keeps 50 ms and init
keeps 100 ms. Progress persists between pushes and a finished provider is never
re-read, so discovery reaches `complete` within three attended pushes on a cold
index and costs about 0.1 s once it is warm. `mm recapture 30d` finishes discovery
in one run (it gets a 30 s discovery budget). While the last push recorded discovery
`partial`, `mm status` names `mm push` until it advises recovery (for example after
three incomplete captures), then names `mm recapture` as the way to finish discovery.
`mm refresh-identity` also runs a complete discovery first,
so it reads the repository-local email of every repository discovery holds. Held
history comes back on the first push after discovery completes, for commits since
the held cursor; only commits older than that cursor, in repositories discovery
finds for the first time, need `mm recapture <window>`.

**Git-root discovery reads more records (Track 102A):** besides Claude Code's project
logs, discovery now finds repositories from the working directories recorded by Codex
(rollouts),
Muse and Antigravity when those providers are enabled, and from Paseo's project list and
the mmm factory's items when they are installed. `mm diag` names the source of each root.
After updating, run `mm push` until `mm diag` shows discovery `complete` (see the
entry above for how long that takes), then run
`mm recapture 30d` once to collect commits from repositories discovery has just found.
Pins in `[retro] repo_roots` for repositories these sources now name can be removed.

**Attended usage reads get 3,000 ms, and autopush gives each provider 1,000 ms concurrently (Tracks 101A / 149A / 165B):** the attended
host-usage default (`host_usage_interactive_budget_ms`) is 3,000 ms,
host reads (sweep plus inline warm) get up to three fifths of the attended scan cap
(`capture_budget_ms` before Track 124A; half before Track 101A), and sequential
attended warm retries keep a reserve of up to 300 ms per later retry. Track
165B gives each enabled reader its own cooperative deadline on autopush.
The default is **1,000 ms** per reader, and an explicit
`host_usage_autopush_budget_ms` now applies to each reader. Readers run
concurrently. Attended push progress shows phase counts and elapsed time,
`mm push --verbose` prints phase timings before the `Push:` outcome lines,
and `mm diag` exposes `push_timings` (including `attended`, `autopush`, and `catchup` runs) and `capture_catchup`. If you pinned `host_usage_interactive_budget_ms`
in `~/.config/mind-meld/config.toml` as a workaround, remove that line and run
`mm push`; the measured reader timings fit the new default. Live verification
across up to three attended pushes follows release installation. Codex also
settles archived rollouts by the date in their name, so rollouts that have not changed
are no longer re-read on later pushes.

**Fast-forward peer edits and propagated deletions (Track 91A):** a peer's edit to a file
you have not changed since you last synced it now simply applies (no union, no conflict
copy), and a file deleted on one machine is now deleted on the others, each keeping a
30-day recovery copy. Before this release a deletion only stopped a missing file from
coming back, and every peer edit to a file both machines held was a conflict.

**Update every machine, then run `mm push` and then `mm pull` on each, before anyone
deletes a file you care about.** Deletions are provable only after a push on this release
has published each folder's marker, and a pull on this release has recorded what each
peer holds; a deletion made before that does not propagate (the first push after updating
mints no deletion records, and a record from an older release carries no content hash, so
it keeps its old effect of stopping a re-download and never deletes). Go one machine at a
time, in any order.

- **Update pinned Codex exclusions before syncing.** If your config has an explicit
  `[[sync.sources]]` entry for `codex`, run `mm migrate-config --dry-run`, then
  `mm migrate-config` on each machine. This appends `plugins/cache/*` and
  `plugins/.remote-plugin-install-staging/*` to its exclusions, preserving your custom
  exclusions. Codex manages these installed plugin files itself. Then run `mm push` on
  each machine; removing them from sync leaves the local files in place and publishes
  no deletion records. Configs using the default sources get these exclusions automatically.
- **What a deletion removes.** A deletion record carries the sha256 of the version that was
  deleted: the newest version this machine held for which it knows the full hash. If the
  full hash is unknown, no record is published. A machine deletes its copy only when that
  file's bytes equal that hash and it
  last synced exactly that version from the machine that deleted it; a file you changed, a
  same-named file with other bytes, or anything else stays. The deleted file is moved to
  `~/.config/mind-meld/deleted/<UTC stamp>-<rand>/<source>/<path>` and kept at least 30
  days. The pull's last line names the folder. See [Recovering a deleted
  file](../../README.md#recovering-a-deleted-file).
- **A fast-forward has no recovery copy.** It replaces your unchanged file with the peer's
  bytes, so a bad edit on one machine spreads to the others. When both machines changed a
  file, `.jsonl` and `MEMORY.md` are still unioned (a line deleted on one side returns when
  both sides changed the file, except a `MEMORY.md` pointer to a missing file that is not
  waiting or failed, or a duplicate pointer for the same file) and any other file still gets
  a conflict copy.
- **A pull no longer restores a file this machine held and removed by hand.** Deleting a
  synced file is a deletion your next push publishes, so an accidental removal spreads at
  the next push; copy it back from a recovery folder (see above) and push from the machine
  that deleted it.
- **Conflict copies that exist today do not clear by themselves.** `mm pull` stops
  reporting a copy it already holds, but `mm conflicts` keeps listing it until you run
  `mm resolve` on each one (it needs a terminal).
- **Pull line-merges older peer edits to `.jsonl` files and `MEMORY.md`.** For other
  files, pull holds a peer edit built on your version with an older mtime. It says
  `N older peer edits not applied`. Per-source pull summaries now use
  `<peer>/<source>` labels. `mm pull --verbose` counts holds by that label and
  lists the held paths.
  `mm diag` shows the last pull's observations, including both mtimes, labeled as
  possibly stale.
  Compare both versions, then run `touch <file>` and `mm push` on the machine whose version
  should win.
- **`.mind-meld-anchor`.** mm keeps this small file (a random token) in each folder it
  scans: for example `~/.claude/projects`, `~/.codex/skills`, `~/.codex/plugins` and
  `~/.gstack/projects`, and the source folder itself (`~/.codex`, `~/.gstack`) for
  sources that sync individual files there. It is never synced; leave it. It is how a push tells "a file was removed" from "the whole
  folder was removed, recreated or restored from a backup". In the second case the push
  mints no deletions and says `N files left out because <folder> is new; run mm pull to
  restore them`. An anchor that cannot be written (a read-only folder) means deletions under
  it will not propagate; the push says so. If a restore tool repeatedly rewrites the token,
  push reports the repeated mismatch and `mm diag` records it; check that tool's restore
  rules rather than repeatedly pushing.
- **Rollback.** The previous release ignores the new `state.db` tables and the new manifest
  fields; files already deleted stay deleted, with their copies. An older release's walker
  publishes `.mind-meld-anchor` from a scan root as an ordinary file that differs on every
  machine, so remove those files before going back, on each machine:

  ```bash
  find ~/.claude ~/.codex ~/.gstack -name .mind-meld-anchor -type f -print -delete
  ```

  Add any other synced source folder that `mm sources` lists.

<a id="track-91a-deletion-recipe"></a>
A two-machine recipe that exercises all of it uses a throwaway file under the default
`gstack` source. Check that `mm sources` shows `gstack` enabled on both Macs. The
push examples assume usage is unchanged and all readers finish.

```bash
# On Mac A, then Mac B (once each, in order)
mm update && mm push           # Push: metadata updated.     (the new .mind-meld-anchor tokens; or N content changes published)
mm pull                        # Pull: no changes.           (or N content changes from Mac A)

# On Mac A: create a file and share it
mkdir -p ~/.gstack/analytics && echo "recipe v1" > ~/.gstack/analytics/mm-recipe.md
mm push                        # Push: 1 content change published.

# On Mac B: receive it
mm pull --verbose              # Includes "flushed N files in X.XXs", then Pull: 1 content change from Mac A.
cat ~/.gstack/analytics/mm-recipe.md   # recipe v1

# On Mac A: delete it, preview, publish
rm ~/.gstack/analytics/mm-recipe.md
mm push --dry-run              # ... - 1 deleted ... Dry run complete. Nothing was changed except the local lock file.
mm push                        # Push: 1 content change published.

# On Mac B: preview, then follow the deletion
mm pull --dry-run              # delete  analytics/mm-recipe.md   ...   1 would be deleted
mm pull                        # Pull: 1 file deleted because Mac A deleted it; copies in ~/.config/mind-meld/deleted/<stamp> for 30 days; see ...#recovering-a-deleted-file.
ls ~/.gstack/analytics/mm-recipe.md    # No such file or directory

# Recover it onto Mac A, the machine that deleted it (a copy put back only on Mac B stays there)
# On Mac B: send the copy to Mac A by any means (scp shown; <stamp> is the folder the pull line named)
scp ~/.config/mind-meld/deleted/<stamp>/gstack/analytics/mm-recipe.md macA:/tmp/mm-recipe.md
# On Mac A: put it back without overwriting anything newer, then publish
cp -n /tmp/mm-recipe.md ~/.gstack/analytics/mm-recipe.md
mm push                        # Push: 1 content change published.

# On Mac B: the file comes back
mm pull                        # Pull: 1 content change from Mac A.
```

Repeat the recipe with the Macs' roles reversed, and note the verbose flush time on each receiving Mac.

**Shared Claude project memory (Track 90A):** Claude keeps a project's memory under a
folder named for the checkout's absolute path, so the same repository at two paths, or
on a Mac and a Linux box, was two projects. mm now publishes a project that has a
network `origin` under that remote, and any other project under a home-relative name,
and each machine maps the name back to the folder Claude uses for *its* checkout, wherever
that checkout is. See [Project memory across machines](../../README.md#project-memory-across-machines).

**Every machine must update before its own first push in the new form, and it takes part
in the new form only from then.** Go one machine at a time, in any order; nothing waits for
the whole fleet. On each machine run the update command, then `mm push`. The first push
after updating that can list `~/.claude/projects` (attended or from a hook) republishes the manifest with shared names;
it uploads no content, deletes nothing and runs no GC. An attended push prints
`Push: project memory published under shared names.`; `mm autopush` stays silent and records
`published under shared names` in its breadcrumb. A push that cannot list the folder holds the switch: `mm status` and an
attended push say project memory is not published, `project_index.reason` is `projects_unlisted`, and the next
push that can list it switches and republishes that machine's project memory as content. A
machine where Claude has not created the folder yet stays on the old form until it does.
Confirm with `mm diag --json`: `project_index.state` is `shared`,
and `project_index.machines` shows which other machines have switched.

- Updated machines sync with each other at once. Older machines keep syncing with each
  other in the old form. An updated machine reads an old-form peer's manifest too: files
  under the same home folder keep applying, and another home's files wait, shown as
  `312 files from Mac B wait until it publishes shared names`, until that peer updates.
- An older machine skips an updated peer's **whole** manifest, every source and not only
  Claude memory, so it receives nothing from that peer until it updates. Through 1.7.1, once
  any peer has updated: `mm gc` refuses at exit 1; an attended `mm push` with a content
  change publishes that content, prints `Push complete.`, then fails with `cannot GC
  safely` at exit 1, every time until that machine updates; blob collection does not run
  there, so orphaned blobs use disk until then; pull, autopull and autopush stay at exit 0.
  Nothing is lost by that, and there is nothing to clean up: update the machine.
- **Through 0.14.18**, run the update command *before* that machine's next attended
  `mm push` or `mm gc`, or leave it off: beside a conflict copy of an updated machine's
  manifest (for example `manifest.json 2.enc`), its GC can read the older copy as that
  machine's whole manifest and delete blobs only the newer manifest references.
- A file deleted on an updated machine keeps flowing among older machines until they update.
- Rollback past the switch is unsupported: an older mm refuses its own new-form manifest, and
  a regression ships as a new release.
- Below v1.3.0 there is no `mm update`, and `pipx upgrade` leaves a tag-pinned install frozen,
  so use `UV_VENV_CLEAR=1 pipx install --force git+https://github.com/kbitz/mind-meld.git@latest`.

A two-machine recipe (Mac A updates first, then Linux B):

```bash
# On Mac A
mm update                      # then: mm --version
mm push                        # Push: project memory published under shared names.
mm status                      # nothing about names unless a peer still waits
mm diag --json | jq .project_index.state   # "shared"

# On Linux B, which keeps a different checkout path
mm update && mm push
git clone git@github.com:acme/widgets.git ~/code/widgets && cd ~/code/widgets
mm pull                        # places the Mac's memory into the folder Claude uses here
ls ~/.claude/projects/*widgets*/memory
```

**`mm init --new-fleet` and the new default storage path (Track 89A).** Starting
a fleet creates its key header (`mm-crypto-init`) in an empty folder. Use
`mm init --new-fleet` or answer `new` at the prompt on the first machine.
A joining machine requires the existing header before it registers the device
or writes to the Mind Meld store. Managed Syncthing setup and its local record
may already be saved while waiting. On a join, `mm init` also runs the first
`mm pull` with the passphrase you just typed. The typed `BRICK` override is
gone. The default storage path is now
`~/Sync/mind-meld`. It does not move an existing store or change a configured
`storage.path`, and configured Macs still on iCloud Drive keep syncing. New or restored
machines now use the Syncthing setup described in Quick Start; `mm init` requires
Syncthing to be running. mm no longer rewrites the key header or
moves a differing copy: repair now only removes `mm-crypto-init` conflict
copies that are byte-identical to the main file. Differing copies stay
where they are (see [Key header copies differ](../../README.md#key-header-copies-differ)),
and `mm diag` reports `pending_repair` as `delete` and `preserve` counts only
(no `replace_canonical`). The iCloud auto-pin step is removed.

**Machine-local gstack files and browser profiles (Track 94A):** any directory
holding a regular, non-symlink `Local State` file, the marker Chromium and Electron write at the
root of a browser user-data directory, is now skipped in every generic source.
No folder name is hard-coded: `chrome-profile`, `signed-profile` and
`.dev/user-data` all qualify. The default gstack `include_patterns` also
narrowed. `manager-*.json`, `*-evidence.jsonl` and the live `driver-handoff.md`
no longer sync (the timestamped `driver-handoff-*.md` archives still do),
`pair-review/` admits only `deploy.md`, `report.md` and `parked-bugs.md`, and
the ten artifact directories admit document file types only. On a Mac whose
config pins the previous default list, `mm migrate-config` offers the
narrowed list, showing only the entries that change; then run `mm push`.

The generic rule replaces a local `projects/*/chrome-profile/*` stopgap
exclude for profiles that still have their marker. Keep the stopgap while
unmarked copies remain (see below); after cleanup you can delete it from
`config.toml`. It is not a recommended glob, so deleting it brings back no
prompt. Narrowing deletes nothing on any
Mac: no tombstones are written, every Mac keeps its copies, and own store
space is reclaimed by an attended push's GC or `mm gc`. To remove profile
copies that already synced:

1. Wait until every Mac runs this release and has pushed once.
2. On each Mac, list marked profiles with
   `find ~/.gstack/projects -name 'Local State' -type f`. Also inspect the
   known profile paths from the originating tools on every Mac: under the new
   artifact allowlist, incoming document files can arrive without `Local State`
   and keep syncing even after every Mac upgrades. Names such as `chrome-profile`,
   `signed-profile` and `user-data` are useful clues, not a complete detector.
3. For each confirmed synced copy, add its exact source-relative directory plus
   `/*` to that source's local `exclude_patterns` on every Mac, including
   Macs without a copy (for example `projects/myapp/designs/t/user-data/*`).
   Then run `mm push` on every Mac and recheck for late arrivals before deleting
   anything, so their manifests drop those files
   without tombstones. Keep these exclusions through cleanup: deleting a marked
   directory also removes its marker, so the marker alone cannot protect the
   next push or prevent stale peer copies from returning.
4. Delete only confirmed copies that no tool still uses, on every Mac. Wait for
   dormant Macs to complete steps 1–3 too. Leave the explicit exclusions in
   place to prevent stale copies returning. Deleting a
   still-published unmarked copy without step 3 can propagate a deletion: a Mac that
   holds identical bytes deletes its copy (with a 30-day recovery copy) once this
   release has run on it.

**Undelivered content is waiting, not failed (Track 87A):** on a Syncthing
folder a manifest can arrive before its blobs, or a file can be replaced while
mm reads it. A blob that has not been delivered yet used to count as a failed
file (attended `mm pull` exit 1, autopull `degraded`). It is now **waiting**:
`mm pull` says `Pull: ... waiting for 2 files from Mac B.` and exits 0, and
`mm autopull` records `success` with the same detail (`mm status` shows it).
Waiting files are not applied; other available files still apply. The next pull
picks up the waiting files once they sync. A failed file still means a real
error (decrypt, write, permission).

Before reading peer manifests, `mm pull` checks delivery for the whole folder
through Syncthing's local read-only API, including on owner-managed folders.
The initial probe has a 2 s REST budget. If delivery is pending, a terminal
pull waits up to `[sync] pull_syncthing_wait_ms` (default 15,000 ms) after the
probe. The transient progress phase is `Waiting for Syncthing to deliver N files`.
See [Sync outcomes](../../README.md#sync-outcomes) for when the spinner appears.
A non-terminal pull or a pull with the setting at `0` proceeds after one probe.
If delivery still appears pending, pull applies what has arrived and adds
`Pull: Syncthing is still delivering N files. Run: mm pull.` Pending delivery
alone keeps exit 0. If a file fails to apply, the error also includes the
pending delivery observation and ends with the repair command.

`mm autopull` makes one probe with a 500 ms REST budget and records pending
delivery in a successful run without printing anything for it. `mm status`
clears the live pending verdict when the folder catches up. The `Last auto-pull`
line shows recorded delivery as a past observation. `mm pull --dry-run` and
the `mm init` join pull skip the delivery check. If the initial check finds
the API unreachable, unauthorized or unconfigured, or finds no Syncthing folder
matching the storage path (for example, iCloud Drive), pull adds no delivery output.

`mm pull` and `mm status` ignore Syncthing's temporary files and `.stversions`
folder and retry observed canonical files that disappear during a read. Device
listing ignores conflict copies and counts a `devices/` file only when its name
matches the id in its body; valid manifest conflict copies remain recovery
inputs. `mm diag`
reports the number of ignored files as `ignored storage files`. See
[waiting content](../../README.md#waiting-content-is-not-a-failure).

**Host usage snapshot compatibility:** an un-upgraded peer drops incoming `host-usage-snapshot` records carrying `"muse"` or `"agy"` until it updates. Content sync is unaffected; upgrade the peer with `mm update` or `pipx upgrade mind-meld` to include its usage in `retro-fleet`.

**Push and pull output (Tracks 78A / 129B / 167A / 171A):** attended `mm push` and `mm pull` end
with one `<Verb>: <what happened>.` outcome line per thing that happened, and
errors print `mm: error: <what broke>` on stderr followed by `Run: <command>` on
its own last line (including CLI usage errors, which exit 2 with
`Run: <command path> --help`). Attended `mm push` appends `; usage published`
only when published usage totals or queued catch-up data changed since this
machine's last published snapshot (`Push: 10 content changes published; usage published.`,
`Push: no content changes; usage published.`, or
`Push: no content changes; usage published (Grok catching up).` when a host reader
hit its deadline). With no verified new usage, idle pushes print
`Push: no content changes; no new usage.`. Content, metadata, selection,
or repair leads omit the usage clause when no readers are catching up. A reader
that stops at its deadline can add `; no new usage (Grok catching up)` to those
leads. Routine capture, cache, budget, nested-repository, scan, and pull
`skipped` details live under `--verbose` (and in `mm status` / `mm diag`);
a pull that only skips newer local files ends with `Pull: no changes.`. Capture
failure does not make content sync fail. Actionable and data-at-risk warnings
still print before the outcome. Successful automatic commands stay silent,
including when they move files.

Update scripts that match the exact `Push: no content changes.` line to
accept the usage clause. After upgrading, refresh the `/retro-fleet` skill
with `mm install-skills` or the next `mm push`, then restart the agent.

**Exit change:** an attended pull with any failed file now exits **1**, replacing
its previous exit 0. Completed files remain applied. `mm autopull` keeps exit 0
for contained failures and records the failed count; conflict-fail preflight
keeps exit 3 before applying files. There is no data/config migration or new
quiet flag. See [Sync outcomes](../../README.md#sync-outcomes), [Exit codes](../../README.md#exit-codes),
[Status verdicts](../../README.md#status-verdicts) and [pull remedies](../../README.md#pull-incomplete--could-not-pull-a-file).
A foreground automatic update can delay the final line; successful version
details appear only with `--verbose`. Standalone `mm update` is unchanged.

**Setup and resolution output (Tracks 130B, 154A):** `mm init`, `mm devices approve`, `mm devices retire`,
source enable/disable/reconfigure and per-conflict resolution now use the same
`<Verb>: <what happened>.` outcomes. Pull and resolution errors, warnings and
notices use the shared stderr forms, including fail-mode preflight warnings.
Human-readable text and streams change; commands, flags, exit codes, consent
and `mm devices --format=json` are unchanged. No migration is needed.

**Status, gc, diff, log, recapture and update-notice output (Track 133A):** `mm gc`
and `mm recapture` now end with one `Gc: ... .` or `Recapture: ... .` outcome line
instead of bold-green "complete" banners. `mm gc -v` shows the per-reaper retention
summaries, and the new `mm recapture --verbose` / `-v` shows the window, repository,
record and size breakdown and the commit-date note. The `mm diff` corrupt-manifest
warning, the incomplete-retention warning, the newer-history-database error, status
advice, and the update nudge and failure notices use the shared `mm: error:` /
`mm: warning:` / `mm: notice:` forms with the fix as `Run: <command>`. Recapture's
errors (including a partial recovery's `mm: error:` headline and its single `Run:`
fix) and its host-usage notice now print on stderr; partial-recovery cause lines
stay on stdout.
Human-readable text and streams change; commands, flags (apart from the new recapture
`-v`), exit codes, stored data and `mm log --format jsonl` records are unchanged. No
migration is needed.

**Empty conflict results (Track 134A):** when they find no conflict files,
`mm conflicts` and `mm resolve` now print `Conflicts: no conflict files.` and
`Resolve: no conflict files.` instead of a green `No conflict files.`, and both
still exit 0. `mm resolve ""` (an empty PATH, such as a script's unset quoted
variable) now fails like any PATH that matches no conflict file
(`mm: error: No conflict file matching: <path>` on stderr, exit 1) instead of
being treated as no PATH, which walks every conflict. No migration is needed.

**Conflict gc and conflict-command lines (Track 136A):** `mm gc --conflicts` no
longer warns about conflict copies it keeps on purpose (unresolved, pre-v0.9.2
or with an unreadable age), and per-reaper summaries show them as `kept=`.
`mm gc` always prints its `Gc:` line, and a real retention failure adds the
warning after it, whose fix after `--conflicts` is `Run: mm gc --conflicts -v`.
An unmatched `mm resolve PATH` ends with `Run: mm conflicts`. `mm conflicts`
ends with `Run: mm resolve`, plus a `mm: notice:` line when it lists pre-v0.9.2
copies. When `mm resolve` cannot read a same-file conflict copy, it names the
move repair only if the original's symlink chain reaches the copy. An uncertain
symlink chain gets advice to inspect both names instead of deleting the copy.
Exit codes and stored data are unchanged, and no migration is needed.


**Status verdict and local run evidence (Track 77A):** `mm status` now opens
its report after authentication with a verdict about this device's last push.
Nested checkout paths are summarized as a count; use `mm diag` for paths.
The optional, private `last-attended-runs.json` needs no migration. Missing
history after upgrade is normal: a readable own manifest and complete local
inspection can establish a healthy comparison without seeding both verbs.
Older binaries ignore this sidecar. Existing auto-update and rollback rules
below still apply; plaintext status is not a stable scripting interface.


**Before the first attended `mm push` or `mm gc` on any Mac, including a dormant
one, check every `mm` it can run.** Older releases' GC can delete other Macs'
blobs during a push or `mm gc`. Run `which -a mm`, then `<path> gc --help` for
each path listed: an mm with own-device GC says "this device's". Update the pipx
install with `mm update` (it updates only pipx installs; an mm older than v1.3.0
has no `mm update`, so use `pipx upgrade mind-meld` as described below). For any
other copy, fix `PATH` so the pipx `mm` comes first and never run that copy
against real storage.
Hooks (`mm autopush`, `mm autopull`) do not run GC, but do not
rely on their background update to bring a Mac current. Updating one Mac protects
peer namespaces only from that Mac's GC; other arrival-order limitations of the
1.x store remain.

**Local history database (Track 73B):** the first history write after upgrading
imports `~/.config/mind-meld/pull-history.jsonl` and `.1` once into local
`state.db`. Before that first write, `mm log` still reads JSONL. New rows go only
to the database; the legacy files remain untouched. The table keeps 20,000 rows
per (verb, action) class, including per-file uploaded rows. The [history
remedies](../../README.md#history-database-statedb) cover interrupted writes and import notices.

Downgrading to an older mm shows only its JSONL history; it cannot see database
rows. After re-upgrading, rows the older binary appended to either legacy file
are **not** imported again. `mm log` notices changes to either file: this shows
past divergence, not proof that an old binary still runs. Check `which -a mm`
and fix PATH on that Mac. To inspect and keep those rows before moving both
legacy files aside (with no mm running):

```bash
jq -c . ~/.config/mind-meld/pull-history.jsonl.1 ~/.config/mind-meld/pull-history.jsonl
mkdir -p ~/mm-history-backup
for name in pull-history.jsonl pull-history.jsonl.1; do
  if [ -f "$HOME/.config/mind-meld/$name" ]; then
    mv "$HOME/.config/mind-meld/$name" "$HOME/mm-history-backup/$name.$(date +%Y%m%d-%H%M%S)"
  fi
done
```

These copies remain readable with `jq -c . ~/mm-history-backup/pull-history*`;
neither the importer nor `mm log` reads backup directories. Keep state.db too
if you need the history recorded by the newer version. Moving only one legacy
file may leave a stale-binary notice for the other.

<a id="v180-scope-trim"></a>

**v1.8.0:** gstack defaults exclude generated output. Complete this checklist
on every active Mac before the next attended `mm pull` on any of them:

1. Before the release lands on each Mac, copy its pull-history files aside.
   Before Track 73B, about 8,000 excluded peer rows per attended pull could
   rotate the shared 1 MB history and evict earlier records. The database now
   keeps 20,000 excluded rows independently of conflict history. Keep existing
   `~/mm-history-backup/` copies: inspect them with `jq -c .
   ~/mm-history-backup/pull-history*`; neither import nor `mm log` reads them.
2. Update each Mac and check `mm --version` (1.8.0 or newer).
3. Run `mm migrate-config`, then `mm push`. A Mac without explicit
   `[[sync.sources]]` reports nothing to migrate; it already uses the new defaults.
4. Run `mm status` on each Mac: no migration warning and no pending gstack deletions.

```bash
# Before v1.8.0 lands on each Mac:
mkdir -p ~/mm-history-backup && cp ~/.config/mind-meld/pull-history.jsonl* ~/mm-history-backup/
# On every active Mac before the next attended pull on any of them:
mm update && mm --version
mm migrate-config
mm push
mm status
```

The first push after migrating includes the removed entries in its content-change count;
`--verbose` shows upload/scan detail. This counts entries removed from this Mac's manifest: expected, no
tombstones, and every Mac keeps its copies. A default-config Mac's autopush
may already have printed `mm: pushed N files (N deleted)` for that trim;
autopush removes no local files and never runs GC. Own storage reclamation
needs an attended content-changing `mm push` or `mm gc`.

Rollback: an install tracking `@latest` can reinstall the latest release on a
later sync command unless `[upgrade] auto_install = false`; a tag-pinned
rollback follows the exception under [Automatic updates](../../README.md#automatic-updates).
A migrated explicit config keeps these globs until removed by hand, which
brings back the migration warning and attended prompt.

**v1.0.0:** no action needed beyond upgrading. Mixed 0.14.x/1.0.0 fleets
interoperate with no wire or storage migration. Newer-format storage refuses with
an upgrade remedy ([Storage-format gates](../invariants/auto-upgrade.md#storage-format-gates)).
`b` / `both` still leave both conflict files on disk. Once v1.0.0 is tagged, a regression
ships as 1.0.1: the upgrade nudge never downgrades.

**v0.14.9:** the old identity cache becomes stale and refreshes on the next identity read that completes repository discovery; incomplete attempts retry.
The first push needing that refresh shows `mm: notice: refreshing identity cache (one-off)` with `--verbose`. [Future capture is corrected; previously published rows stay unchanged.](../../README.md#dropped-repositories-and-ignored-git-environment-variables)

For `mm: error: Pull incomplete [result-degraded]:` or a per-file warning after upgrading, see [pull failures and remedies](../../README.md#pull-incomplete--could-not-pull-a-file). No data migration is needed for apply exception containment.
