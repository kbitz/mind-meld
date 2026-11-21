# Cross-model memory

Status: designed on 2026-11-21 from the developer's decisions of 2026-11-20
and 2026-11-21. This is [docs/SPEC.md](../SPEC.md) O9, planned now and a
condition of 2.0.0, with its own MINOR checkpoint for a 1.x release that
lands it first. Qualified on 2026-11-21 on `linux-a`
([qualification](#qualification)): Q1 and Q2 passed on the first run and Q3
kept the default Codex copy set; Tracks 157A, 159A and 160A may start. This
design replaces the 2026-10-29
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
| Codex | Under `CODEX_HOME/memories/`, written only when the `memories` feature is on. codex-cli 0.171.0 lists it as `memories stable false`, so it is off by default, and it is off on `linux-a`. With codex-cli 0.164.0 on `mac-a` (2026-10-29), the folder held `memory_summary.md`, `MEMORY.md` (in `# Task Group: <project> <task>` sections), `raw_memories.md`, `rollout_summaries/`, `extensions/` and a `.git` that Codex commits to itself. Codex regenerates `MEMORY.md` and `memory_summary.md` wholesale. It also keeps `memories_1.sqlite` (consolidation jobs) | No |
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

Each machine regenerates its own Codex memory, so syncing those files as
shared files would conflict on every pull. Instead, a new `codex-memory`
source works like this:

- At push, mm reads this machine's Codex memory files from
  `CODEX_HOME/memories/` without changing them, and publishes them as this
  machine's files. The default file set is `memory_summary.md`, `MEMORY.md`
  and `rollout_summaries/*.md`. Codex's `.git`, its SQLite files and anything
  else outside that set never sync. Q3 kept this default set on 2026-11-21
  ([qualification](#qualification)); codex-cli 0.171.0 also initializes
  `CODEX_HOME/memories_v2/` with the same layout.
- At pull, mm places each peer's files under
  `~/.local/share/mind-meld/codex-memory/<device id>/`. That folder is mm's,
  outside every native store.
- Each file has one writer, its source machine, so copies never conflict.
- Forgetting follows the source machine. When its Codex drops a memory, the
  next push publishes the smaller set, and truth-based manifests remove it
  from every peer.
- Copies from a machine that leaves the fleet (`mm devices retire`) leave
  every peer.
- mm never writes into `CODEX_HOME`.
- `mm init` offers the source when `CODEX_HOME/memories/` exists, with Enter
  = yes, as for other detected sources. `mm enable-source codex-memory`
  enables it later.
- A machine with Codex memories off publishes nothing. Its agents still find
  every other machine's memories through the index.

### 3. One local index per machine

When the memory index is on, mm keeps qmd collections in qmd's default index:

| Collection | Folder | Mask |
|---|---|---|
| `mm-claude` | `~/.claude/projects` | `*/memory/**/*.md` |
| `mm-grok` | `~/.grok/memory-v2` | `**/topics/**/*.md` |
| `mm-muse` | `<Muse root>/memory` | `**/*.md` |
| `mm-codex-<this machine's name>` | `CODEX_HOME/memories` | the Q3 file set (default set kept: `memory_summary.md`, `MEMORY.md`, `rollout_summaries/*.md`) |
| `mm-codex-<peer name>` | `~/.local/share/mind-meld/codex-memory/<peer id>` | `**/*.md` |

- A collection exists only for an enabled source whose folder exists.
- mm adds, updates and removes only collections whose names start with
  `mm-`. The owner's own qmd collections are never touched.
- Names come from mm device names through `safety`, reduced to
  `[a-z0-9-]`.
- The markdown is the truth. The index is derived, stays on the machine and
  is never synced.

### 4. Refresh after every sync

- Attended `mm push` and `mm pull` run `qmd update` after the sync lock is
  released (about 0.2 s here). When embeddings are on, they then start
  `qmd embed` in the background.
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

The final pointer line, settled by Q1 and Q2 on 2026-11-21 without revision:

> Before assuming a fact about this project or the owner's preferences,
> search agent memory with `qmd search "<key words>" -n 10` (every agent,
> every machine; `qmd get <docid>` reads a hit). If memories disagree, show
> the owner each one with its path and ask which is right; never pick one
> silently.

- Setup shows the line and the global instruction files it belongs in:
  `~/.claude/CLAUDE.md` and `CODEX_HOME/AGENTS.md`. On the owner's yes, it
  appends the line once to each regular file inside the home folder.
- For a file that is a symlink, or that resolves outside the home folder,
  mm prints the line and the resolved path and changes nothing. On the
  owner's machines both files link into a config repository: the owner adds
  the line there once, and that repository carries it to every machine.
- mm never edits an agent's MCP configuration. Setup prints
  `claude mcp add qmd -- qmd mcp` and `codex mcp add qmd -- qmd mcp` for
  owners who want MCP. The line works through the CLI alone.

### 6. Contradictions show up in one result list

A search returns every matching memory from every provider and machine. Each
hit is labelled by collection, which names the provider and, for Codex, the
machine.

Example from a temporary `HOME`: `mm-claude` held "Use PostgreSQL 16 for the
ledger" and `mm-codex-mac-a` held "The ledger uses SQLite, not Postgres".
`qmd search ledger` listed both hits, `qmd://mm-codex-mac-a/MEMORY.md` and
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

Control searches from the harness returned
`qmd://mm-claude/-home-user-ledger/memory/export.md:2 #be9616` for
`"ledger nightly export"`,
`qmd://mm-codex-mac-a/MEMORY.md:1 #8aa6a7` for `"ledger fixtures"`, and both
`qmd://mm-claude/-home-user-ledger/memory/db.md:2 #627ce9` and
`qmd://mm-codex-mac-a/MEMORY.md:5 #cea051` for `"ledger database"` and
`"ledger"`.

#### Q1 and Q2 sessions

Codex sessions ran via
`codex exec --skip-git-repo-check -C "$ROOT/work" --json -o <file> -s workspace-write --add-dir "$ROOT/home/.cache/qmd"`
(note for Track 160A: `qmd search` and `qmd get` succeeded inside
`workspace-write` with `--add-dir` for the qmd cache folder). Claude Code
sessions ran in `$ROOT/work` via
`claude -p --output-format stream-json --verbose --no-session-persistence --allowedTools "Bash(qmd *)" "Bash(qmd:*)" -- <prompt>`
(an initial CLI invocation without `--` before the prompt exited immediately
with a flag-parsing error before starting a session, because `--allowedTools`
is variadic). Neither agent ran `qmd query`, `qmd vsearch`, or `qmd embed`,
and transcript scans showed no file access outside `$ROOT` other than
`/usr/bin/bash` and `/dev/null`.

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
`d04dc37 Initialize Codex git baseline` after S1 through S4:

| Path under `CODEX_HOME` | S0 → S1 | S1 → S2 | S2 → S3 | S3 → S4 |
|---|---|---|---|---|
| `auth.json` | unchanged (name only) | unchanged (name only) | unchanged (name only) | unchanged (name only) |
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
| `models_cache.json` | new (604152 B, `307bffc5`) | regenerated wholesale (`399d9d98`) | regenerated wholesale (`5ad5d9e2`) | regenerated wholesale (`765e0957`) |
| `state_5.sqlite` | new (307200 B, `2ee7bb81`) | regenerated wholesale (`006ef6b5`) | unchanged (WAL active) | regenerated wholesale (315392 B, `03d52580`) |
| `thread_history_1.sqlite` | new (118784 B, `86f93e8e`) | regenerated wholesale (122880 B, `80da121f`) | regenerated wholesale (126976 B, `fffd3fc7`) | regenerated wholesale (131072 B, `f4a3db8e`) |
| `logs_2.sqlite` | new (49152 B, `b5dcf171`) | unchanged | unchanged (WAL active) | regenerated wholesale (503808 B, `6923e2e4`) |

Because Codex extracted no rollout into `stage1_outputs` and wrote no new
memory markdown within the four-session budget, the default copy set
(`memory_summary.md`, `MEMORY.md`, `rollout_summaries/*.md`) is kept, excluding
`.git`, `.codex-baseline`, SQLite databases, lockfiles, `raw_memories.md`, and
static `extensions/ad_hoc/instructions.md` boilerplate. Track 157A should
note that `codex-cli 0.171.0` initialized `CODEX_HOME/memories_v2/` on disk.

#### Final pointer line and outcome

Final pointer line (settled without revision):

> Before assuming a fact about this project or the owner's preferences,
> search agent memory with `qmd search "<key words>" -n 10` (every agent,
> every machine; `qmd get <docid>` reads a hit). If memories disagree, show
> the owner each one with its path and ask which is right; never pick one
> silently.

All credential files inside `$ROOT` and `$ROOT` itself were deleted after the
runs; raw transcripts and snapshots are kept off the repository on `linux-a`.
**Outcome:** Q1 and Q2 passed and Q3 kept the default copy set. Tracks 157A,
159A and 160A may start.
