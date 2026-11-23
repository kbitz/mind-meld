# Cross-model memory

Status: designed on 2026-11-21 from the developer's decisions of 2026-11-20
and 2026-11-21. This is [docs/SPEC.md](../SPEC.md) O9, planned now and a
condition of 2.0.0, with its own MINOR checkpoint for a 1.x release that
lands it first. On 2026-11-21, Q1 and Q2 passed on `linux-a` and Q3 kept the
default Codex copy set ([qualification](#qualification)). This clears the
qualification prerequisite for Tracks 157A, 159A and 160A. The Tracks still
follow their roadmap dependency order. This design replaces the 2026-10-29
design ([archived](../archive/cross-model-memory-2026-10-29.md)), which
carried only Codex memory and pointed agents at grep. The developer rejected
it: "o9 - why just codex? current design seems like we should throw out and
start over". [Memory continuity](memory-continuity.md) and the
[Codex memory contract](codex-memory-contract.md) remain Track 68A evidence.

## Goal

Anything one agent learned on any of the owner's machines can be found by
every agent on every machine. That covers Claude Code, Codex, Grok Build,
Muse, and any agent that can run a command or call an MCP server. One local
search index covers every provider's memory. mm never copies a memory into
another agent's store.

## The developer's decisions

Verbatim, 2026-11-20 and 2026-11-21:

- "my thinking around o9 is that maybe the right setup is that locally we set
  up a store that incorporates all provider memories, and agents can query it
  w/ qmd? as we sync memories across machines, they will get incorporated into
  local store naturally, or removed"
- "reaching for the search tool -- we would want to recommend a line in
  agents.md as part of setup"
- "we would want to facilitate installed qmd just like we do syncthing on
  setup"
- "memories contradicting each other is valuable as it gives us the
  opportunity to correct the conflict"
- On three open questions: "1 c / 2 a / 3 a". O9 moves before 2.0, with its
  qualification and implementation on the Current Plan. Keyword search is the
  baseline, and embeddings are opt-in.
- Asked whether "1 c" made O9 a condition of 2.0.0: "3 yes, gate".

## What exists today

Verified on 2026-11-20 on `linux-a` (Arch, mm 1.12.73), unless noted.

| Provider | Memory on disk | Synced by mm today |
|---|---|---|
| Claude Code | `~/.claude/projects/<project>/memory/*.md` (344 files on `linux-a`) | Yes, per project by git remote since 1.12.0 |
| Grok Build | `~/.grok/memory-v2/global/topics/` and `~/.grok/memory-v2/workspaces/<scope>/topics/` (28 topic files). `MEMORY.md` and the SQLite files stay local | Yes, topics only (`manifest.GROK_MEMORY_ROOT`) |
| Muse | `memory/` under the Muse source root, by default `~/.local/share/muse` (`config.DEFAULT_MUSE_DIR`). None on `linux-a` | Yes, when enabled |
| Codex | Codex CLI 0.171.0 lists `memories stable false`. The feature is off by default and off on `linux-a` outside the qualification. With it enabled, [Q3](#qualification) observed `CODEX_HOME/memories_v2/`. Codex CLI 0.164.0 on `mac-a` (2026-10-29) used `CODEX_HOME/memories/` and regenerated `MEMORY.md` and `memory_summary.md` wholesale. That older folder also held `raw_memories.md`, `rollout_summaries/`, `extensions/` and a `.git` that Codex commits to itself. `MEMORY.md` used `# Task Group: <project> <task>` sections. Codex kept consolidation jobs in `memories_1.sqlite`. | No |
| Antigravity | No memory store, only rules and skills | Not applicable |

qmd 2.8.3 is the npm package `@tobilu/qmd`. It needs Node 22 or newer and
also runs under Bun. On `linux-a` it is installed globally with Bun
(`~/.cache/.bun`) and linked from `~/.local/bin/qmd`. Its index is
`~/.cache/qmd/index.sqlite`, and its collections are listed in
`~/.config/qmd/index.yml` (both follow XDG). The following was measured in a
temporary `HOME` on `linux-a` (16 cores, load average about 10):

- Keyword search (`qmd search`, BM25) needs no model.
- `qmd embed`, `qmd vsearch` and `qmd query` download GGUF models from
  Hugging Face on first use: an embedding model (334 MB), a reranker (639 MB)
  and a query-expansion model (1.28 GB).
- `qmd collection add <dir> --name <n> --mask '*/memory/**/*.md'` indexes
  only memory files. It refuses a folder that does not exist.
- `qmd update` re-indexes every collection and drops deleted files.
- Speed: adding a 400-file collection took 0.5 s, `qmd update` took 0.2 s
  whether or not files had changed, `qmd search` took 0.16 s, and a bare
  start took 0.14 s.
- `qmd mcp` serves the same index over MCP (stdio).
- Each search hit names its collection and path
  (`qmd://<collection>/<path>`) and a docid that `qmd get` reads.

Both global instruction files on `linux-a`, `~/.claude/CLAUDE.md` and
`~/.codex/AGENTS.md`, are symlinks into the owner's config repository. mm's
Codex source syncs `AGENTS.md`. Claude's `CLAUDE.md` stays in git.

## Design

### 1. Each provider's memory keeps its own sync

mm keeps syncing Claude project memory, Grok memory topics and Muse `memory/`
exactly as today. Same-provider sync is unchanged: no file moves between
providers.

### 2. Codex memory roams as per-machine copies

Each machine regenerates its own Codex memory. Mind Meld publishes per-machine
copies using the `codex-memory` manifest key. Since Track 161A, the copies
follow the `codex` source.

- At push, mm reads this machine's active Codex memory directory without
  changing it, and publishes the files under this machine's device ID. Q3
  kept the default file set of `memory_summary.md`, `MEMORY.md` and
  `rollout_summaries/*.md` ([qualification](#qualification)). Codex's `.git`,
  its SQLite files and anything else outside that set stay local.
- Publishing and the local qmd collection use `memories_v2/` when it is
  a real directory and fall back to `memories/`. Q3 on a fresh 0.171.0
  home observed `memories_v2/` alone.
- At pull, mm places each peer's files under
  `~/.local/share/mind-meld/codex-memory/<device id>/`. That folder is mm's,
  outside every native store.
- Each copy has one publisher. An edit on a receiving machine can still
  conflict with a publisher update.
- Forgetting follows the source machine. When its Codex drops a memory, the
  next push publishes the smaller set, and truth-based manifests remove it
  from every peer.
- A pull with `codex` enabled moves a retired machine's copies into
  30-day local recovery.
- Copy sync leaves the native Codex memory directory untouched.
- `mm init` offers the `codex` source. `mm enable-source codex` enables
  customization sync and per-machine memory copies on an existing setup.
- With `codex` enabled, a machine can receive and index peer copies even
  when it has no native Codex memory directory.

### 3. One local index per machine

When the memory index is on, mm keeps qmd collections in a dedicated
`mind-meld` qmd index (`--index mind-meld`, stored at
`~/.cache/qmd/mind-meld.sqlite` and `~/.config/qmd/mind-meld.yml`):

| Collection | Folder | Mask |
|---|---|---|
| `mm-claude` | `~/.claude/projects` | `*/memory/**/*.md` |
| `mm-grok` | `~/.grok/memory-v2` | `**/topics/**/*.md` |
| `mm-muse` | `<Muse root>/memory` | `**/*.md` |
| `mm-codex-<this machine's name>` | The active Codex memory directory from section 2 | `memory_summary.md`, `MEMORY.md`, `rollout_summaries/*.md` |
| `mm-codex-<peer name>` | `~/.local/share/mind-meld/codex-memory/<peer id>` | `**/*.md` |

- A collection exists only for an enabled source whose folder exists.
- Every routine qmd operation passes `--index mind-meld`, so qmd's default
  index and any project-local `.qmd/index.yml` are never updated, embedded,
  or touched on fresh installs or routine runs. Only the one-time upgrade
  cleanup removes legacy `mm-` collections from `--index index` after the
  dedicated index refreshes cleanly.
- mm adds, updates and removes only collections whose names start with
  `mm-`. The owner's own qmd collections are never touched.
- Names come from mm device names through `safety`, reduced to
  `[a-z0-9-]`.
- The markdown is the truth. The index is derived, stays on the machine and
  is never synced.

### 4. Refresh after every sync

- Attended `mm push` and `mm pull` run `qmd --index mind-meld update` after
  the sync lock is released (about 0.2 s here). When embeddings are on, they
  then start `qmd --index mind-meld embed` in the background.
- `mm autopush` and `mm autopull` start the same refresh detached and never
  wait (O8.2, O8.3).
- Before refreshing, mm reconciles its collections with the enabled sources
  and current fleet machines.
- Synced additions and deletions therefore reach search on the next sync,
  and so does a memory an agent just wrote locally (at the session-end hook).
- A refresh failure never changes a sync's outcome. `mm status` reports it
  with its fix, and `mm diag` shows qmd's version, collections and last
  refresh.

The developer's words name the refresh after a pull. The refresh after a push
is added so that one agent's new memory reaches the other agents on the same
machine.

### 5. Agents search, guided by one recommended line

The canonical pointer line uses `--index mind-meld` and narrows the trigger
to when the owner mentions something previously discussed with an agent:

> If the owner mentions something they discussed with an agent before and you
> can't find it in your own memory, search every agent's memory with
> `qmd --index mind-meld search "<key words>" -n 10`
> (`qmd --index mind-meld get <docid>` reads a hit). If memories disagree,
> show the owner each one with its path and ask which is right.

- Setup shows the line and the global instruction files it belongs in:
  `~/.claude/CLAUDE.md` and `CODEX_HOME/AGENTS.md`. On the owner's yes, it
  appends the line once to each regular file inside the home folder.
- For a file that is a symlink, or that resolves outside the home folder,
  mm prints the line and the resolved path and changes nothing. On the
  owner's machines both files link into a config repository: the owner adds
  the line there once, and that repository carries it to every machine.
- After successful `mm memory-index on`, mm prints the shared O8 outcome line
  linking directly to the README's [Agent setup](../../README.md#agent-setup)
  subsection.
- mm never edits an agent's MCP configuration. Optional MCP commands for the
  `mind-meld` index live in [Agent setup](../../README.md#agent-setup). The
  pointer line works through the CLI alone.

### 6. Contradictions show up in one result list

A search returns every matching memory from every provider and machine. Each
hit is labelled by collection, which names the provider and, for Codex, the
machine.

Example from a temporary `HOME`: `mm-claude` held "Use PostgreSQL 16 for the
ledger" and `mm-codex-mac-a` held "The ledger uses SQLite, not Postgres".
`qmd --index mind-meld search ledger` listed both hits,
`qmd://mm-codex-mac-a/MEMORY.md` and
`qmd://mm-claude/<project>/memory/db.md`. The pointer line tells the agent to
show both and ask.

The owner corrects the wrong memory at its source:

- A Claude, Grok or Muse memory file: edit it, and sync carries the change.
- A Codex memory: tell Codex on the machine that wrote it, which regenerates
  it.

The next sync and refresh then carry the correction. mm does not detect,
rank or merge contradictions itself.

### 7. Setup installs qmd the way it installs Syncthing

- **`bin/bootstrap`.** After Syncthing, bootstrap installs qmd when
  `qmd --version` does not run. It uses Bun when present, otherwise npm
  with a per-user prefix. If neither exists, it installs Node with pacman
  on Arch or Homebrew on macOS. It never downloads models and never needs
  sudo for qmd itself. A failure prints the manual command and does not stop
  the mm install.
- **`mm init`.** When qmd runs, init asks
  `Index agent memories for search with qmd? [y/n]` with Enter = yes, then
  creates the collections and runs the first `qmd update`. When qmd is
  missing, init prints the install command and skips the index.
- **Embeddings.** A separate question,
  `Also download qmd's embedding model (334 MB) for semantic search? [y/n]`,
  takes Enter = no. Yes runs `qmd embed` in the background.
- **Pointer line.** Init offers the line as in section 5.
- **Later.** `mm memory-index` turns the index on or off and shows its
  state. Off removes only `mm-` collections.
- **Configuration.** The index and embedding choices live in this machine's
  `config.toml` and never sync.

### 8. Privacy

The index and qmd's model cache stay on the machine. Codex copies are
encrypted in the store like all synced content (O4). Search runs locally,
and only the opt-in model download reaches the network. qmd sends it no
memory text.

## Out of scope

- Writing into any provider's native memory store, including Codex's.
- Syncing the index.
- Automatic contradiction detection or merging.
- Requiring semantic search.
- Indexing Antigravity, which has no memory store.
- Feeding foreign memories into Codex's own consolidation. codex-cli 0.171.0
  lists `external_agent_memory_import` as under development and off.
  Revisit when it ships as stable.

## Qualification

Implementation Tracks are blocked until this passes. Each check runs in
isolated homes: a temporary `HOME`, `CODEX_HOME` and XDG folders, never the
owner's real stores or index. Sessions authenticate without copying the
owner's credential files into the temporary home, through an API key in the
environment or a fresh login there.

- **Q1, recall through qmd.** Seed a Claude-origin memory and a Codex-origin
  memory in their planned folders, and index them with the planned
  collections. The temporary `CLAUDE.md` and `AGENTS.md` hold only the
  pointer line. A fresh Codex session answers a question that only the
  Claude memory answers, by running `qmd search`. A fresh Claude Code session
  finds the Codex memory the same way.
- **Q2, contradiction.** Seed two contradicting memories from different
  providers. Each fresh session reports both, with their paths, and asks the
  owner instead of picking one.
- **Q3, Codex file set.** In an isolated `CODEX_HOME` with `memories` on,
  record which files Codex writes and which it regenerates wholesale, then
  choose the copy set. If Codex writes no memory within the session budget,
  record that and keep the default set.

Record in this document the results, the observed codex-cli, Claude Code
and qmd versions, the commands the agents ran and the final pointer line.
A failed Q1 or Q2 returns the design to the owner before any implementation
Track starts.

### Results, 2026-11-21

Observed inside the isolated environment on `linux-a`: `codex-cli 0.171.0`,
`2.1.330 (Claude Code)`, `qmd 2.8.3`, and `node v26.12.0`.

#### Isolation and authentication

- Temporary root `$ROOT` created with `mktemp -d /var/tmp/mm-156b.XXXXXX`
  (mode `0700`). Layout: `$ROOT/home` (`HOME`), `$ROOT/home/.codex`
  (`CODEX_HOME` for Q1 and Q2), `$ROOT/home/.config` (`XDG_CONFIG_HOME`),
  `$ROOT/home/.cache` (`XDG_CACHE_HOME`, placing the qmd index at
  `$ROOT/home/.cache/qmd/index.sqlite`), `$ROOT/home/.local/share`
  (`XDG_DATA_HOME`, placing Codex copies at
  `$ROOT/home/.local/share/mind-meld/codex-memory/mac-a-00000000/`),
  `$ROOT/home/.local/state` (`XDG_STATE_HOME`), `$ROOT/bin` (symlinks to the
  resolved `codex`, `claude`, and `qmd` binaries, bypassing `mise` shims),
  `$ROOT/work` (empty non-git working directory for Q1 and Q2),
  `$ROOT/q3-codex` (`CODEX_HOME` for Q3, with no `AGENTS.md`), and
  `$ROOT/q3-work` (Q3 working directory holding one 3-line `project.txt`).
- Every `qmd` command and agent session ran through `env -i` with an explicit
  allowlist: `HOME`, `CODEX_HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`,
  `XDG_DATA_HOME`, `XDG_STATE_HOME`, `PATH=$ROOT/bin:/usr/bin:/bin`,
  `SHELL=/bin/bash`, `LANG`, and `TERM`.
- Ancestor check: `/var/tmp`, `/var`, and `/` held no `CLAUDE.md`,
  `AGENTS.md`, `.claude`, or `.git`.
- Empty-index check before adding collections: `qmd collection list` printed
  `No collections found. Run 'qmd collection add .' to create one.` and
  `qmd status` reported `$ROOT/home/.cache/qmd/index.sqlite` with
  `Total: 0 files indexed` and `Vectors: 0 embedded`.
- Owner qmd `stat` before the first `qmd` command and after `$ROOT` deletion
  matched identically: `/home/user/.cache/qmd/index.sqlite` (`size=122880`,
  `mtime=1793057842`) and `/home/user/.config/qmd/index.yml` (`size=270`,
  `mtime=1793057842`).
- Authentication route: fresh subscription logins inside each temporary home
  (`claude auth login --claudeai` in `$ROOT/home`; `codex login --device-auth`
  in `$ROOT/home/.codex` and `$ROOT/q3-codex`). No API keys were read or
  exported, and no credential files were copied from `/home/user`.

#### Seeds and collections

`$ROOT/home/.claude/projects/-home-user-ledger/memory/export.md`:

```markdown
---
name: ledger nightly export
description: Schedule and output path for the ledger nightly export
type: project
---

The ledger's nightly export runs at 03:40 UTC and writes `exports/ledger-<date>.parquet`.
```

`$ROOT/home/.claude/projects/-home-user-ledger/memory/db.md`:

```markdown
---
name: ledger database
description: Database engine for the ledger project
type: project
---

Use PostgreSQL 16 for the ledger.
```

`$ROOT/home/.local/share/mind-meld/codex-memory/mac-a-00000000/MEMORY.md`:

```markdown
# Task Group: ledger fixtures

Ledger fixtures are regenerated with `make ledger-fixtures SEED=8812`.

# Task Group: ledger database

The ledger uses SQLite, not Postgres.
```

Collections built (`mm-grok` and `mm-muse` were not seeded):

```bash
qmd collection add "$ROOT/home/.claude/projects" --name mm-claude --mask '*/memory/**/*.md'
qmd collection add "$ROOT/home/.local/share/mind-meld/codex-memory/mac-a-00000000" --name mm-codex-mac-a --mask '**/*.md'
qmd update
```

Control searches from the harness found
`qmd://mm-claude/-home-user-ledger/memory/export.md` for
`"ledger nightly export"`, `qmd://mm-codex-mac-a/MEMORY.md` for
`"ledger fixtures"`, and both `qmd://mm-claude/-home-user-ledger/memory/db.md`
and `qmd://mm-codex-mac-a/MEMORY.md` for `"ledger database"` and `"ledger"`.

#### Q1 and Q2 sessions

The final pointer line below was the sole content of
`$ROOT/home/.codex/AGENTS.md` and `$ROOT/home/.claude/CLAUDE.md`.

Codex sessions ran via
`codex exec --skip-git-repo-check -C "$ROOT/work" --json -o <file> -s workspace-write --add-dir "$ROOT/home/.cache/qmd"`
(on `linux-a`, `qmd search` and `qmd get` succeeded with that cache grant).
For Track 160A, this evidence covers the granted-cache configuration on
Linux. Claude Code sessions ran in `$ROOT/work` via
`claude -p --output-format stream-json --verbose --no-session-persistence --allowedTools "Bash(qmd *)" "Bash(qmd:*)" -- <prompt>`
(an initial CLI invocation without `--` before the prompt exited immediately
with a flag-parsing error before starting a session, because `--allowedTools`
is variadic). The allow rule preapproved the matching Bash calls. The
recorded commands also include directory and file checks inside `$ROOT`.
The transcripts showed no tool call accessing the owner's stores and no
`qmd query`, `qmd vsearch`, or `qmd embed` calls.

| Session | Prompt | Commands run (from transcript) | Answer excerpt | Verdict |
|---|---|---|---|---|
| Codex, Q1 | `In the ledger project, when does the nightly export run and where does it write its file? Answer in two sentences.` | `rg --files --hidden -g "!**/.git/**"`; `qmd search "ledger nightly export schedule file destination" -n 10`; `qmd search "export" -n 10`; `qmd search "nightly export" -n 10`; `qmd search "ledger" -n 10`; `qmd get '#be9616'` | "The ledger's nightly export runs at 03:40 UTC. It writes to `exports/ledger-<date>.parquet`." | **PASS** |
| Claude Code, Q1 | `How do I regenerate the ledger fixtures? Answer in one sentence.` | `qmd search "regenerate ledger fixtures" -n 10; ls -la $ROOT/work; cat $ROOT/home/.claude/projects/-var-tmp-mm-156b-XXXXXX-work/memory/MEMORY.md 2>/dev/null`; `qmd search "ledger fixtures" -n 10; qmd search "ledger-fixtures SEED" -n 10; qmd get "#cea051"` | "Run `make ledger-fixtures SEED=8812`. This comes from a saved agent memory note (`qmd://mm-codex-mac-a/MEMORY.md`), and I couldn't check it against the Makefile because the working directory is empty." | **PASS** |
| Codex, Q2 | `Which database does the ledger use? Answer briefly.` | `qmd search "ledger database" -n 10`; `qmd get '#cea051'`; `qmd get '#627ce9'` | "Agent memories disagree. - **PostgreSQL 16** — `qmd://mm-claude/-home-user-ledger/memory/db.md` - **SQLite** — `qmd://mm-codex-mac-a/MEMORY.md` Which is correct?" | **PASS** |
| Claude Code, Q2 | `Which database does the ledger use? Answer briefly.` | `qmd search "ledger database" -n 10`; `qmd get "#627ce9"; echo ----; qmd get "#cea051"` | "Two saved memories give different answers, so I can't tell you which one is right: - `qmd://mm-claude/-home-user-ledger/memory/db.md` says: 'Use PostgreSQL 16 for the ledger.' - `qmd://mm-codex-mac-a/MEMORY.md` says: 'The ledger uses SQLite, not Postgres.' Which is correct?" | **PASS** |

#### Q3: Codex memory files and copy set

Four sessions (~186 s of session runtime, ~13 minutes wall clock) ran with
`CODEX_HOME=$ROOT/q3-codex` and `--enable memories -c memories.min_rollout_idle_hours=0`
(accepted without error; `logs_2.sqlite` recorded `feature.memories="true"`,
`memories.generate_memories="true"`, `memories.use_memories="true"`,
`memories.max_rollouts_per_startup="2"`, and
`memories.max_raw_memories_for_consolidation="256"`). S1 and S2 ran via
`codex exec`; after inspecting `state_5.sqlite` showed that Codex's interactive
thread index filters on
`source IN ('cli', 'vscode', '{"custom":"atlas"}', '{"custom":"chatgpt"}')`
whereas `codex exec` records `source = 'exec'`, S3 and S4 ran via the
interactive `codex` CLI (`--no-alt-screen --no-daemon`, recording
`source = 'cli'`).

In S1, `codex-cli 0.171.0` initialized `CODEX_HOME/memories_v2/` (with `.git`
at commit `d04dc37 Initialize Codex git baseline`), `memories_1.sqlite`,
`memories_v2_1.sqlite` (with one completed `memory_consolidate_global` job and
`0` rows in `stage1_outputs`), and `memories_v2.lock`; no `CODEX_HOME/memories/`
directory was created. `git -C memories_v2 log --oneline` remained at
`d04dc37 Initialize Codex git baseline` after S1 through S4. The table
summarizes selected paths from the full snapshots. A change is classified
as appended when the prior bytes are a prefix of the new bytes. Other byte
changes say nothing about whether Codex replaced the file or edited it in
place.

| Path under `CODEX_HOME` | S0 → S1 | S1 → S2 | S2 → S3 | S3 → S4 |
|---|---|---|---|---|
| `auth.json` | present (name only) | present (name only) | present (name only) | present (name only) |
| `memories_1.sqlite` | new (45056 B, `62a1452a`) | unchanged | unchanged | unchanged |
| `memories_v2_1.sqlite` | new (45056 B, `23024163`) | unchanged | unchanged | unchanged |
| `memories_v2.lock` | new (0 B, `e3b0c442`) | unchanged | unchanged | unchanged |
| `memories_v2/memory_summary.md` | new (79 B, `a57aae3d`) | unchanged | unchanged | unchanged |
| `memories_v2/extensions/ad_hoc/instructions.md` | new (733 B, `d36a3608`) | unchanged | unchanged | unchanged |
| `memories_v2/.codex-baseline/.git/codex-baseline.lock` | new (0 B, `e3b0c442`) | unchanged | unchanged | unchanged |
| `memories_v2/.git/HEAD`, `config`, `index`, `refs/heads/main`, `logs/*`, `objects/*` | new (baseline commit `d04dc37`) | unchanged | unchanged | unchanged |
| `config.toml` | — | — | new (42 B, `0772345e`) | unchanged |
| `history.jsonl` | — | — | new (366 B, `d1b0dbfa`) | appended (536 B, `4588a2e4`) |
| `session_index.jsonl` | — | — | new (135 B, `c28e0f97`) | appended (277 B, `58f81059`) |
| `sessions/2026/11/21/rollout-*.jsonl` (1 per session) | S1 new (154456 B, `f4f9e8bc`) | S2 new (141393 B, `98567fd6`) | S3 new (149766 B, `39dd9395`) | S4 new (140260 B, `82889027`) |
| `models_cache.json` | new (604152 B, `307bffc5`) | non-append change (`399d9d98`) | non-append change (`5ad5d9e2`) | non-append change (`765e0957`) |
| `state_5.sqlite` | new (307200 B, `2ee7bb81`) | non-append change (`006ef6b5`) | main file unchanged (WAL active) | non-append change (315392 B, `03d52580`) |
| `thread_history_1.sqlite` | new (118784 B, `86f93e8e`) | non-append change (122880 B, `80da121f`) | non-append change (126976 B, `fffd3fc7`) | non-append change (131072 B, `f4a3db8e`) |
| `logs_2.sqlite` | new (49152 B, `b5dcf171`) | unchanged | main file unchanged (WAL active) | non-append change (503808 B, `6923e2e4`) |
| SQLite `-wal` and `-shm` sidecars | — | — | new for state, logs and both memory databases | changed for state and logs, removed for both memory databases |

Codex left `stage1_outputs` empty and its bootstrap `memory_summary.md`
unchanged after S1. The four sessions produced no consolidated memory from
the seeded facts or observed markdown regeneration. Q3 kept the default
copy set of `memory_summary.md`, `MEMORY.md` and `rollout_summaries/*.md`.
`.git`, `.codex-baseline`, SQLite databases, lockfiles, `raw_memories.md`
and static `extensions/ad_hoc/instructions.md` stay local.

#### Final pointer line and outcome

Pointer line used in Q1 and Q2 (settled without revision during qualification):

> Before assuming a fact about this project or the owner's preferences,
> search agent memory with `qmd search "<key words>" -n 10` (every agent,
> every machine; `qmd get <docid>` reads a hit). If memories disagree, show
> the owner each one with its path and ask which is right; never pick one
> silently.

All credential files inside `$ROOT` and `$ROOT` itself were deleted after
the runs. The retained transcripts and snapshot metadata were screened for
credentials and kept outside the repository on `linux-a`. The provider
sessions can remain valid after local deletion. The owner can revoke them
in the provider account settings.

**Outcome:** Q1 and Q2 passed and Q3 kept the default copy set. The
qualification prerequisite for Tracks 157A, 159A and 160A is met.

### Results, 2026-11-22 (Track 163A)

Observed inside an isolated temporary root (`/var/tmp/mm-163a.XXXXXX`, mode
`0700`, `env -i` allowlist matching the 2026-11-21 protocol, `$ROOT/bin`
symlinks bypassing `mise` shims) on `linux-a`: `codex-cli 0.171.0`,
`2.1.330 (Claude Code)`, `qmd 2.8.3`, and `node v26.13.1`.

- **Seeds:** The nightly export fact and PostgreSQL claim were in Claude's
  `qmd://mm-claude/-home-user-ledger/memory/export.md` (`#be9616`) and `db.md`,
  respectively. The fixture regeneration command and SQLite claim were in the
  Codex peer copy, `qmd://mm-codex-mac-a/MEMORY.md`.
- **Named-index isolation:** `qmd --index mind-meld collection add` and
  `qmd --index mind-meld update` created only
  `$ROOT/home/.cache/qmd/mind-meld.sqlite` and
  `$ROOT/home/.config/qmd/mind-meld.yml`; no default `index.sqlite` or
  `index.yml` was created. Owner `stat` on `/home/user/.cache/qmd/index.sqlite`
  (`size=1945600`, `mtime=1795400030`) and `/home/user/.config/qmd/index.yml`
  (`size=427`, `mtime=1795314017`) matched identically before and after the
  runs and `$ROOT` deletion.
- **Codex sandbox probe (Criterion 5):** Under `codex sandbox -C "$ROOT/work"`
  with read access to `/` and write access limited to `$ROOT/work` (no grant
  to `$ROOT/home/.cache/qmd`):
  - `qmd --index mind-meld search "ledger nightly export" -n 10` exited `1`
    with `SqliteError: unable to open database file` (`SQLITE_CANTOPEN`) in
    `enableWal` (`@tobilu/qmd/dist/db.js:77`).
  - `qmd --index mind-meld get "#be9616"` exited `1` with the same
    `SQLITE_CANTOPEN` error in `enableWal`.
  - Adding write access solely to `$ROOT/home/.cache/qmd` (`~/.cache/qmd`)
    allowed both `qmd --index mind-meld search` (exit `0`) and
    `qmd --index mind-meld get "#be9616"` (exit `0`) to succeed.
- **Authentication and sandbox settings (Criterion 6):** Fresh subscription
  logins inside the temporary home (`codex login --device-auth` in
  `$ROOT/home/.codex`; `claude auth login --claudeai` in `$ROOT/home`); no
  owner credential files were read or copied.
  - Codex sessions ran via
    `codex exec --skip-git-repo-check -C "$ROOT/work" --json -o <file> -s workspace-write`
    with `[sandbox_workspace_write]` `writable_roots = ["$ROOT/home/.cache/qmd"]`
    in `$ROOT/home/.codex/config.toml`.
  - Claude Code sessions ran in `$ROOT/work` via
    `claude -p --output-format stream-json --verbose --no-session-persistence --allowedTools "Bash(qmd --index mind-meld search:*)" "Bash(qmd --index mind-meld get:*)" -- <prompt>`.
- **Final pointer line** (sole content of `$ROOT/home/.codex/AGENTS.md` and
  `$ROOT/home/.claude/CLAUDE.md`):

> If the owner mentions something they discussed with an agent before and you
> can't find it in your own memory, search every agent's memory with
> `qmd --index mind-meld search "<key words>" -n 10`
> (`qmd --index mind-meld get <docid>` reads a hit). If memories disagree,
> show the owner each one with its path and ask which is right.

| Session | Prompt | Commands run (from transcript) | Answer excerpt | Verdict |
|---|---|---|---|---|
| Codex, Q1 | `Earlier I discussed the ledger nightly export with an agent, but I cannot remember the details. When does the ledger nightly export run and where does it write its file? Answer in two sentences.` | `qmd --index mind-meld search "ledger nightly export" -n 10`; `qmd --index mind-meld get '#be9616'` | "The ledger nightly export runs every night at 03:40 UTC. It writes its file to `exports/ledger-<date>.parquet`." | **PASS** |
| Claude Code, Q1 | `I discussed regenerating the ledger fixtures with another agent before. How do I regenerate the ledger fixtures? Answer in one sentence.` | checked own memory dir; `qmd --index mind-meld search "regenerate ledger fixtures" -n 10`; `qmd --index mind-meld search "ledger fixtures" -n 10`; `qmd --index mind-meld search "fixtures seed" -n 10` | "Run `make ledger-fixtures SEED=8812`, according to another agent's notes in the shared memory index (`qmd://mm-codex-mac-a/MEMORY.md`, since my own memory had nothing on this)." | **PASS** |
| Codex, Q2 | `I discussed the ledger database with an agent before — which database does the ledger use? Answer briefly.` | `qmd --index mind-meld search "ledger database" -n 10`; `qmd --index mind-meld get '#627ce9'`; `qmd --index mind-meld get '#cea051'` | "The agent memories disagree. - **PostgreSQL 16** — `qmd://mm-claude/-home-user-ledger/memory/db.md` - **SQLite** — `qmd://mm-codex-mac-a/MEMORY.md` Which is correct?" | **PASS** |
| Claude Code, Q2 | `I discussed the ledger database with an agent before — which database does the ledger use? Answer briefly.` | checked own memory dir; `qmd --index mind-meld search "ledger database" -n 10`; `qmd --index mind-meld get "#627ce9"; echo ----; qmd --index mind-meld get "#cea051"` | "I found two saved memories about this, and they disagree: 1. `mm-claude/-home-user-ledger/memory/db.md` says: 'Use PostgreSQL 16 for the ledger.' 2. `mm-codex-mac-a/MEMORY.md` (under 'ledger database') says: 'The ledger uses SQLite, not Postgres.' Which one is right?" | **PASS** |

The transcripts showed zero owner-store touches and zero `qmd query`,
`qmd vsearch`, or `qmd embed` calls. All temporary credentials and `$ROOT`
were deleted after the runs.

**Outcome:** Q1 and Q2 passed in both directions with the revised pointer line
and `--index mind-meld`, covering O9.6's isolated evidence. The owner's Mac
and Linux fleet acceptance remains separate.
