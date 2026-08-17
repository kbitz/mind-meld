# Host interchangeability (Claude / Codex / Grok)

> Written 2026-08-17 after Track 21A review feedback. Supplements
> `SPEC.md` and `docs/designs/grok-build-usage-reader.md`. Does not
> reopen Tracks 18D or 21A.

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

| Capability | Claude | Codex | OpenCode | Grok |
|---|---|---|---|---|
| Usage totals on the MODELS card | Session jsonl walk (priced) | Host snapshot, source-gated | Host snapshot, source-gated | 18D reader + 21A consent; 22A/23A render |
| Customization roaming | `memory/` + `todos/` only. `CLAUDE.md` / agents / commands stay git-tracked | Allowlisted `skills/`, `plugins/`, `AGENTS.md` | Allowlisted agents / commands / modes / plugins / skills / tools / `AGENTS.md` | Allowlisted `skills/`, `commands/`, `rules/` via `type: "grok"` |
| Sessions snapshot (repos, counts, skill names) | Yes. Local walk; no transcript bytes on the wire | No | No | No |
| `retro-fleet` skill link | `~/.claude/skills` | `~/.codex/skills` | `~/.config/opencode/skills` | Not installed. New design; see Plan C |
| Session / transcript sync | Never | Never | Never | Never |

Claude is not the template for "sync the home directory." Claude does
not sync `~/.claude/projects/**/*.jsonl`. Codex and OpenCode only roam
because they have a documented customization subtree that is not
credentials or chat. Grok's installed root is the opposite mix.

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

Still excluded pending a separate review:

- `hooks/` — scripts and HTTP endpoints; may embed tokens
- `plugins/` — Codex syncs plugins; Grok auto-trusts `~/.grok/plugins/`,
  so a synced plugin is immediately trusted on the pulling Mac

Never include: `sessions/`, `auth.json`, `mcp_credentials.json`,
`config.toml`, `trusted_folders.toml`, `logs/`, `memtrace/`,
`worktrees/`, `marketplace-cache/`, `active_sessions.json`,
`last-copy.txt`, `bin/`, `bundled/`, `vendor/`.

Repo-level `.grok/` and `AGENTS.md` stay git-tracked, the same way
Claude's project `CLAUDE.md` is not an mm source.

The ship gate was a written allowlist in `DEFAULT_SOURCES`, a hardcoded
walker, and tests proving that `sessions/`, `auth.json`, `config.toml`, and a
nested `skills/<link> → sessions/` upload zero files.

### Plan C — Grok `retro-fleet` skill link (new design)

**Not a sync source.** Extend `skill_link.SKILL_ROOTS` with
`~/.grok/skills` (or `$GROK_HOME/skills` at call time) as a fourth
`SkillTarget`. Same no-clobber state machine, own 24h markers, own
status line.

Grok already discovers `~/.claude/skills` when Claude compatibility
is on, so a fleet that only uses Claude's skill dir already shares
`retro-fleet`. Plan C is for Grok-only machines and for installs that
disabled Claude compat.

Do not widen the shipped source or invent another Grok sync source to make the
installer "complete."

### Explicitly not planned

- Uploading Grok, Codex, OpenCode, or Claude session transcripts.
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

- Usage reader + consent: `docs/designs/grok-build-usage-reader.md`
- Writer / consumer contract: `docs/invariants/events-retro.md`
  (host-usage snapshot + sessions-snapshot sections)
- Default allowlists that B must resemble: `config.py:DEFAULT_SOURCES`
  (`codex`, `opencode`)
- Execution: `docs/ROADMAP.md` Groups 18, 21, 22, 23; Future items
  for B and C
