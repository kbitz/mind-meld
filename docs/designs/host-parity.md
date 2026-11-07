# Host interchangeability (Claude / Codex / Grok)

> Written 2026-08-17 after Track 21A review feedback. Supplements
> `SPEC.md` and `docs/archive/grok-build-usage-reader.md`. Does not
> reopen Tracks 18D or 21A.
>
> **Superseded in part (v1.1):** retro-fleet now renders one `## Agents` table,
> not the MODELS card described below. See README and
> `docs/invariants/events-retro.md`.
>
> **Host scope (2026-10-19):** [docs/SPEC.md](../SPEC.md) owns which hosts mm
> supports. Standalone Grok Build support is being removed, Muse gets its own
> `muse` family, and Grok models used through Cursor stay in the Grok row. The
> matrix below is historical where it disagrees.
>
> **Grok Build restored (2026-11-05):** the owner brought Grok Build back at
> parity with Claude Code and Codex; see
> [Grok Build parity](#grok-build-parity-2026-11-05) below and docs/SPEC.md's
> table.

## Outcome

`retro-fleet` treats Claude, Codex, and Grok as interchangeable **usage
families** on one MODELS card. It does not treat them as interchangeable
**sync trees**. Session transcripts stay local on every host.

Track 21A established the local usage-consent bit. Track 22B extends the
same `mm enable-source grok` verb with a narrow, Claude-shaped customization
source: `skills/`, `commands/`, and `rules/` only. It never walks the Grok
home root or session transcripts.

## Why this exists

Track 18D is the Grok equivalent of the Codex usage reader: a strict,
metadata-only walk of completed-turn records. Track 21A gates that walk
behind local consent and publishes the existing encrypted
`host-usage-snapshot`. Tracks 22A and 23A put accepted host totals on the
same card as Claude session tokens, with coverage labels instead of silent
zeros.

That is the interchangeability the 21A review asked for. The remaining
gaps are different products, and collapsing them into 18D/21A would
either upload prompts or mint a fake `grok` sync source.

## Capability matrix

| Capability | Claude | Codex | Grok | Cursor (Conductor + enrolled CLI) | Muse |
|---|---|---|---|---|---|
| Usage totals on the MODELS card | Session jsonl walk (priced) | Host snapshot, source-gated | 18D reader + 21A consent; 22A/23A render | 67A consented run ledger plus enrolled standalone completions; model-family rows | Host snapshot, source-gated; `muse-*` models render under Unclassified in 1.x (family deferred to 2.x) |
| Customization roaming | `memory/` + `todos/` only. `CLAUDE.md` / agents / commands stay git-tracked | Allowlisted `skills/`, `plugins/`, `AGENTS.md` | Allowlisted `skills/`, `commands/`, `rules/` via `type: "grok"` | None; no user-authored tree observed | Allowlisted `memory/` via `type: "muse"` |
| Sessions snapshot (repos, counts, skill names) | Yes. Local walk; no transcript bytes on the wire | No | No | No | No |
| `retro-fleet` skill link | `~/.claude/skills` | `~/.codex/skills` | None. Grok 1.0.5 discovers `~/.claude/skills` via default-on Claude-compat (`grok inspect --json`). `mm diag` reports that under `host_skill_discovery`, not a fourth `skill_links` row. See Plan C | None; discovers ~/.claude/skills with third-party extensibility enabled | None; discovers ~/.claude/skills (`muse skills list` resolves user skills there) |
| Session / transcript sync | Never | Never | Never | Never | Never |

Claude is not the template for "sync the home directory." Claude does
not sync `~/.claude/projects/**/*.jsonl`. Codex only roams because it
has a documented customization subtree that is not credentials or chat.
Grok's installed root is the opposite mix.

## What 18D / 21A / 22A / 23A already are

```text
18D  strict Grok terminal-ledger reader + private cache
  |
21A  local consent bit + all-or-nothing snapshot
  |
22B  allowlisted Grok customization source; same enable/disable verb
  |
22A  aggregator accepts latest complete host snapshot per device
  |
23A  MODELS card: Claude sessions + coverage-aware Codex/Grok/other rows
```

- **18D** equals Codex `read_codex_usage`, not Claude `walk_session_metadata`.
- **21A** introduced the local usage opt-in. **22B** makes `grok` a real,
  hardcoded allowlist too; source-enabled is consent, while the 21A bit stays
  as a compatibility OR for prior opt-ins.
- **22A/23A** are the card-level interchangeability. Until they land,
  a published Grok snapshot is encrypted fleet data that retro does not
  yet render.

Do not add a zero-file `grok` sync source to make the CLI "look the
same." It would create an empty remote manifest source, generate
legacy unknown-source noise, and imply that Grok files are synced.

## Why we do not sync `~/.grok`

A live 1.0.4 home (`~/.grok`, 2026-08-17) holds credentials and
conversation state at the root:

- `auth.json`, `config.toml`, `trusted_folders.toml`, `agent_id`
- `sessions/` (`updates.jsonl` is a mixed ACP stream; `chat_history.jsonl`
  is prompts and responses; plus plans, rewind points, tool output)
- `active_sessions.json`, `logs/`, `memtrace/`, `worktrees/`,
  `marketplace-cache/`, `last-copy.txt`

Grok's documented customization locations (`skills/`, `commands/`,
`rules/`, `hooks/`, `plugins/`) are real, but they are optional and
were absent on that install. They are not a reason to walk the root.
`config.toml` can carry MCP and model settings the same way Codex's
whole-file config is refused.

`mm enable-source grok` now adds the narrow `type: "grok"` source and keeps
the 21A usage bit enabled. Reusing the one name is safe because the walker
cannot be widened with `include_dirs` and never enters the home root.

## What we still do not walk

Claude's events tail also builds a `sessions-snapshot`: which repos,
how many sessions, which skill names. That walk stays on the Mac.
The wire row is counts and names, not transcripts.

Codex rollouts and Grok session directories are not that ledger.
Their on-disk cwd is an encoded path. Putting it on the wire would
publish filesystem layout. Inferring "projects" by decoding those
paths is a new privacy design, not a missed 18D task.

Until a host publishes a **metadata-only project index** (no path, no
prompt, no tool output), Codex and Grok do not get a sessions
snapshot. Absence on the card stays unlabeled-or-Claude-only, never
a guessed repo list.

## Plans

### Plan A — Grok rows on the MODELS card (already scheduled)

Tracks 22A and 23A. No new design. Host totals stay out of Claude
API cost estimation. Missing, opted-out, or stale devices stay
unknown, not zero.

This is the answer to "why can't I see Grok next to Claude and Codex?"

### Plan B — Allowlisted Grok customizations (shipped: Track 22B)

**Not 18D. Not 21A.** Track 22B shipped the reviewed customization source.

Source name: `grok`. Type: `grok`. Same DEFAULT_SOURCES shape as Claude (`name` / `path` / `type`). The walker hardcodes the allowlist — there is no user-editable `include_dirs` to widen onto sessions.

Shipped allowlist:

| Include | Why it is a candidate | Open question |
|---|---|---|
| `skills/` | Documented user skill tree; same shape as Codex | Generated host links and every nested symlink are excluded |
| `commands/` | Documented user slash-command tree | Only this tree is walked |
| `rules/` | Home-level `*.md` rules (`$GROK_HOME/rules/`) | Only this tree is walked |
| `memory-v2/global/topics/` and `memory-v2/global/observations/_inbox/` | Global curated topic `*.md` files and pending observation `*.md` files (Track 111A) | Only singly-linked lowercase `*.md` files with no dot-segment below `memory-v2/` |
| `memory-v2/workspaces/<scope>/topics/` and `memory-v2/workspaces/<scope>/observations/_inbox/` | Per-workspace curated topic `*.md` files and pending observation `*.md` files (Track 111A) | Only non-symlink `<scope>` directories; `MEMORY.md`, `index.sqlite*`, `memory_state.sqlite*`, `archive/`, and `.retired-stores/` stay local |

Still excluded pending a separate review:

- `hooks/` — scripts and HTTP endpoints; may embed tokens
- `plugins/` — Codex syncs plugins; Grok auto-trusts `~/.grok/plugins/`,
  so a synced plugin is immediately trusted on the pulling Mac

Never include: `sessions/`, `auth.json`, `mcp_credentials.json`,
`config.toml`, `trusted_folders.toml`, `logs/`, `memtrace/`,
`worktrees/`, `marketplace-cache/`, `active_sessions.json`,
`last-copy.txt`, `bin/`, `bundled/`, `vendor/`, and inside `memory-v2/`:
`MEMORY.md`, `index.sqlite*`, `memory_state.sqlite*`, `archive/`,
`observations/` outside `observations/_inbox/`, and `.retired-stores/`.

Repo-level `.grok/` and `AGENTS.md` stay git-tracked, the same way
Claude's project `CLAUDE.md` is not an mm source.

The ship gate was a written allowlist in `DEFAULT_SOURCES`, a hardcoded
walker, and tests proving that `sessions/`, `auth.json`, `config.toml`, and a
nested `skills/<link> → sessions/` upload zero files.

#### Track 111A Live Check (`grok 1.0.77`, 2026-11-06, 15 headless invocations)

Verified in an isolated scratch `GROK_HOME` (`~/scratch/grok-111a/h1`, deleted after the check) with `GROK_MEMORY=1` and `GROK_AUTH_PATH="$HOME/.grok/auth.json"`:

- **(a) & (a2) Repository workspace naming (`Branch A`):** Two clones of `mind-meld` (`origin = https://github.com/kbitz/mind-meld`) at `repos/clone1/mind-meld` and `repos/other/path/mind-meld`, a clone with a renamed checkout directory (`repos/mind-meld-renamed`), a repository with the same `origin` URL and a different root commit (`repos/diff-root/mind-meld`), and a clone using the SCP remote form (`git@github.com:kbitz/mind-meld.git`) all resolved to `memory-v2/workspaces/mind-meld-ac6e5bf3/` (matching `memory_v2_server_store.repo_key = 'kbitz/mind-meld'` and the owner's `~/.grok/memory-v2/workspaces/mind-meld-ac6e5bf3/`). A clone with the same root commit and a different `origin` (`https://github.com/example/other-repo.git`) resolved to `other-repo-921e1414`.
- **(b) Linked git worktrees:** A `git worktree add` checkout (`repos/wt-checkout`) of clone 1 resolved to the same `mind-meld-ac6e5bf3` workspace scope.
- **(c) Cross-`HOME` portability:** Running with `HOME` pointed at a separate scratch directory (`fakehome/Documents/Dev/Personal/mind-meld`, `origin = https://github.com/kbitz/mind-meld`) resolved to the same `mind-meld-ac6e5bf3` workspace scope.
- **(d) No-origin git repository and non-repository folder:** A git repository with no `origin` remote (`repos/no-origin-repo`) set `meta.device_local = '1'` and hashed its canonical path (`no-origin-repo-c5609387` under `/home/kb/...`, `no-origin-repo-6afae551` under `fakehome/...`); a plain directory (`repos/plain-dir`) resolved to `plain-dir-a1142273`.
- **(e) Observation capture, filenames, modes, and staging files:** After a session turn followed by `grok --resume <sessionId> -p "/flush"`, Grok wrote one observation file per extracted observation at `observations/_inbox/01a1b2fa-2e16-7681-9696-703c57c0e141__t000001-000001__n000.md` (`schema_version: 2`, mode `0600`, `st_nlink == 1`, YAML frontmatter with `session_id`, `from_turn`, `through_turn`, `job_id`, `observation_ordinal`, `observation_count`). Manual/tool notes in `observations/_inbox/` use `schema_version: 1` (`note_kind`, `origin`, `created_at`). Every temporary file observed during turn capture, `/flush`, and `/dream` used a leading-dot `.tmp<6alnum>` name (e.g., `.tmp8lqqbW`, `.tmpRibr4Y`), which `mm`'s dot-segment and `.md`-suffix rules reject without extra patterns.
- **(f) Cold-start adoption:** Seeding `workspaces/mind-meld-ac6e5bf3/topics/seed.md` in a fresh `GROK_HOME` with no `MEMORY.md`, `index.sqlite`, or `memory_state.sqlite` caused Grok on first launch to create `MEMORY.md` (`0600`), `index.sqlite` (`0644`), and `memory_state.sqlite` (`0644`) and list `- **Seed Conventions** (\`topics/seed.md\`)` in `MEMORY.md`.
- **(f2) Warm-store adoption:** Writing `topics/seed2.md` and updating `topics/seed.md` via temp file + `os.replace` + `os.utime` to an `mtime` 1 hour older than `MEMORY.md` caused Grok on the next session launch in clone 2 to reconcile `MEMORY.md` to list both `seed.md` and `seed2.md` and recall the updated `VIOLET-9921-UPDATED` and `COBALT-4410-NEW` values without `/dream` or deleting `MEMORY.md`/`index.sqlite`.
- **(g) Folder modes:** Grok accepted `0755` (`umask`-default) scope and `topics/`/`observations/_inbox/` directories and created its own `global/`, `workspaces/<scope>/`, `topics/`, `observations/`, `observations/_inbox/`, and `archive/` directories with mode `0755`. No `pullflow.py` directory-mode change is required.
- **(h) Dream consolidation and hard-link check:** Every file written by Grok had `st_nlink == 1`. Running `/dream` consolidated pending inbox notes into `topics/<slug>.md`, updated `MEMORY.md`, and moved the consolidated file from `observations/_inbox/` into `archive/dream_<id>/` (version-1 inbox notes are scanned directly from `observations/_inbox/` on disk; version-2 `__t...__n` captures are tracked alongside `capture_observation_files` in `memory_state.sqlite`). Placing an already-archived file back into `observations/_inbox/` and running a second `/dream` printed `Nothing to consolidate.` (`consolidation_archives` deduplicates it so topic facts are not duplicated) and left the re-created file in `observations/_inbox/` — confirming the need for the inbox held rule in `pullplan.py`.
- **(i) Anchor and unmapped workspace tolerance:** `memory-v2/.mind-meld-anchor` (`0600`) and an unmapped `memory-v2/workspaces/unmapped-deadbeef/topics/unmapped.md` remained untouched across all 15 invocations with no errors or warnings.

#### Track 111A review follow-up (Grok 1.0.77, 2026-11-06)

The original check above does **not** establish adoption of transferred session
captures. Review repeated the missing warm-store case with synthetic data in two
fresh scratch `HOME`/`GROK_HOME` pairs outside `/tmp`, using the same fixture
repository and `origin`. No personal memory, session or credential content was
read or copied; Grok alone used its existing login. The receiver was initialized
before transfer, and its `index.sqlite` and `memory_state.sqlite` files existed
(their contents were not opened).

The sender generated one `schema_version: 2` observation through a normal turn
and `/flush`. Review copied only that observation Markdown, byte-for-byte, into
the receiver's matching `mind-meld-ac6e5bf3/observations/_inbox/` directory. The
file's SHA-256 on both sides was
`5c5296c7d5270faf83db47fbbb7e6eaa9157f6eb28f7581b9cfe5e13e4f786fd`.
The receiver's `/dream` exited 0 with `Nothing to consolidate.`, created no
topic, and left the transferred observation unchanged in its inbox. As a positive
control, the sender's `/dream` exited 0 with `Dream merged 1 observation into 1
topic.`, wrote the synthetic fact into a topic and emptied its inbox. This proves
that copying ordinary session-capture Markdown alone does not satisfy the
approved cross-machine Dream-adoption requirement on this version. The Track
remains blocked at its live adoption gate; database sync or another adoption
mechanism is not authorized by this result.

This follow-up used five invocations, bringing Task 1 to 22 of its 24-call cap
(15 original, two no-origin identity checks, five adoption checks). All owned
scratch homes and the fixture repository were removed afterwards. Separately,
the no-origin checks confirmed path-dependent scopes, and the owner accepted
inert no-origin/non-repository copies as a reduced scope. That identity decision
does not waive the transferred-observation adoption gate. The original Branch A
label above applies only to the observed matching origin-derived scopes.

### Plan C — Grok `retro-fleet` skill discovery (measured, not linked)

**Not a sync source. Not an `AgentRow`.** Grok 1.0.5 already loads
`retro-fleet` from `~/.claude/skills` via its default-on Claude
compatibility layer, at the same documented priority tier as
`~/.grok/skills` (Grok README "Skill Locations"). Verified 2026-08-24
with `grok inspect --json`:

```text
name: retro-fleet
source.path: /Users/kb/.claude/skills/retro-fleet/SKILL.md
vendor: claude
compatibilityStatus: enabled
externalCompat claude/skills: true default
```

mm therefore maintains **no** Grok skill link. `skill_link.py` line 7
forbids a parallel agent-name list; a second registry of non-linked
hosts is the same mistake. Exit criterion, as a decision procedure
rather than a property of hosts: **mm maintains a skill link only for
hosts that do not discover `~/.claude/skills`.** Re-check with
`grok inspect --json`. Codex shows no evidence of that discovery
(`~/.codex/AGENTS.md` mentions are the user's own instruction prose —
and gstack's `setup --host auto` plus `machine-setup` copy into that
dir).

What `mm diag` reports instead is `host_skill_discovery`: whether Grok
can load the skill. That is a sibling JSON key, never a fourth
`skill_links` row (`skill_links` drives the `mm status` nag and the
README uninstall loop). The probe is `mm diag` only — never status,
push, or autopush.

Two Grok levers, and they are not interchangeable:

- `[skills] ignore` (Grok README) is the documented lever that
  *breaks* discovery of `~/.claude/skills`.
- `[skills] paths = ["~/.local/share/mind-meld/agent-skills"]` is the
  remedy that *adds* the mm-owned store.
- The Claude-compat-off toggle itself is **undocumented** in Grok
  1.0.5 — a live `~/.grok/config.toml` can carry
  `[compat.claude] hooks = false`, and that key appears nowhere in
  Grok's README — so `grok inspect --json` → `externalCompat` is the
  only reliable read.

`$GROK_HOME` stays a `host_usage` sessions-only override. Do not reuse
`config.grok_customization_dirs_exist` as a presence probe: it is False
when `skills/`/`commands/`/`rules/` are absent under `~/.grok`, which
is this exact Mac, while Grok is installed and loading the skill.

Do not widen the shipped source or invent another Grok sync source to make the
installer "complete."

### Explicitly not planned

- Uploading Grok, Codex, or Claude session transcripts.
- Parsing Grok `chat_history.jsonl`, `signals.json`, `summary.json`,
  or `updates.jsonl` content-bearing shapes as a fallback usage
  source. 18D already refuses those.
- A Codex/Grok `sessions-snapshot` built by decoding cwd paths.
- Estimating Grok or Codex subscription/API cost from host counters.
- Treating `mm enable-source grok` as whole-home or session-file sync.

## Sequencing

```text
Group 18 (18D)     reader              ← shipped v0.12.34
    ↓
Group 21 (21A)     consent + publish   ← shipped v0.12.34
    ↓
Track 22B           Grok customization source   ← shipped
    ↓
Group 22 / 23      card
    ↓
Plan C              Grok skill link
```

Plan C is independent of the usage-card work. It is not a prerequisite for
22A/23A, and it does not widen the shipped source.

## Pointers

- Usage reader + consent: `docs/archive/grok-build-usage-reader.md`
- Writer / consumer contract: `docs/invariants/events-retro.md`
  (host-usage snapshot + sessions-snapshot sections)
- Default allowlists that B must resemble: `config.py:DEFAULT_SOURCES`
  (`codex`)
- Execution: `docs/ROADMAP.md` Groups 18, 21, 22, 23; Future items
  for B and C


## Cursor via Conductor (Track 67A)

`[retro] cursor_host_usage = true` authorizes reading Conductor's metadata-only
`runs.ndjson`. Bare cursor-agent has no persisted billing ledger. **Superseded
in part:** `mm enable-source cursor` now enrolls a `stop` hook and
`mm cursor-agent` wraps print runs, so future standalone completions are
captured (see `docs/invariants/events-retro.md`, "Standalone Cursor capture");
sessions before enrollment remain outside coverage. **Superseded for
stores:** since Conductor 0.90.1 the same reader opens each workspace
`index.db` read-only and selects run identity, status, model, the terminal
timestamp and the token counters (see README "Conductor SQLite stores"). It
still never reads Conductor's Claude/Codex copy (would double-count existing
corpora), prompts, results, transcripts, agents.ndjson, checkpoints or usage
APIs, and it never writes a store file.

No Cursor sync source: the measured skills directory contains generated links,
not user files. Revisit only when a user-authored skill or command exists.
No AGENT_ROWS entry: Cursor already discovers ~/.claude/skills, follows the
mm-owned store link, and has third-party extensibility enabled by default.
Grok Build remains exempt for the same reason. No new host family: Grok 4.7
joins Grok, while unobserved auto/composer remain Unclassified.

## Muse (1.x minor, 2026-10-14)

`mm enable-source muse` adds a Claude-shaped source (`type: "muse"`,
`~/.local/share/muse`) whose walker hardcodes `memory/` — Muse's
`personal/` and per-project memory notes. Sessions, the session index,
bundled skills and plugin caches at the same root never roam, and
`~/.config/muse` (auth, settings, trust, the config-repo-deployed
`AGENTS.md`) is not a source at all. Source-enabled is the usage
reader's consent, matching Codex and Grok; there is no retro bit. Unlike
Grok's user-authored customization dirs, Muse creates `memory/` itself on
first run (holding only its `.muse-memory.lock` PID file, which never
roams), so the source never auto-activates: explicit enable only.

The reader counts terminal `model_completed` events in
`sessions/YYYY/MM/DD/<id>/session.jsonl` (plus `subagent/` descendants)
under the strict Grok discipline; `goal_usage_attribution` records
restate the same steps and are never read. Muse writes about ten times
Grok's volume (~100 logs a day at the census), so settled day directories
freeze into per-day/model totals that are only stat-checked afterwards;
see the events/retro invariants. The census
and counter semantics are `tests/fixtures/host_sessions/muse/CONTRACT.md`.

No new host family in 1.x: the compatibility contract makes a family a
MAJOR (an un-upgraded peer rejects the whole snapshot over an unknown
nested family), while the `muse` reader/token-source name is a MINOR.
`muse-*` models therefore render under Unclassified until the 2.x
contract rewrite adds the family. No AGENT_ROWS entry: Muse discovers
`~/.claude/skills` (the Plan C exit criterion), verified with
`muse skills list --source user --json` resolving user skills there.

## Grok Build parity (2026-11-05)

The owner, 2026-11-05: "go, it needs to bring grok to parity w/ the other
models". Grok Build now runs through Paseo's `grok` provider (ACP) on the same
subscription as Cursor, which meets the revisit trigger of the 2026-10-19
removal. Each row of docs/SPEC.md's table, compared with Claude Code and
Codex, on Grok Build 1.0.70 (this Mac, 2026-11-05):

| Row | Claude Code / Codex | Grok Build |
|---|---|---|
| Usage | Local session logs (Claude) and rollouts (Codex), source-gated | Local session records under `~/.grok/sessions/<cwd>/<session>/`: `updates.jsonl` carries one `turn_completed` record with usage per turn, and `usage.json` holds session totals, per-model `modelUsage` and per-turn rows with `endedAt`. Runs through Paseo write them too, unlike Cursor's ACP runs. Paseo also leaves empty sessions (a two-line `updates.jsonl`, no usage) that read as zero. Consent is `mm enable-source grok`, as for Codex. Tokens are priced at list rates like every other host; Grok's own `costUsdTicks` is subscription accounting and is not published. |
| Customizations | Codex: `skills/`, `plugins/`, `AGENTS.md` | `skills/`, `commands/`, `rules/` (Grok's home rules play the part of Codex's `AGENTS.md`); the walker is hardcoded as Track 22B shipped it. Plugins and hooks stay local: Grok trusts `~/.grok/plugins/` automatically and hooks may embed tokens, and none exist on the owner's machines. |
| Memory | Claude: project memory and `todos/`, a project keyed by its git remote | `memory-v2/global/` and `memory-v2/workspaces/<project>-<hash>/`: each scope's `topics/` and `observations/_inbox/`. The generated `MEMORY.md`, SQLite stores, search index and `archive/` stay local. Whether the `<hash>` names the same repository on every machine is not yet known; if it does not, workspace memory is mapped by git remote as Claude's is. |
| Skill link | Claude and Codex links | None: Grok reads `~/.claude/skills` (Plan C still holds). |
| Settings | Whole-file settings stay local | `config.toml`, `auth.json`, `trusted_folders.toml` and the rest of the home root stay local. |
| Sessions snapshot | Claude only | None, as for Codex. |

