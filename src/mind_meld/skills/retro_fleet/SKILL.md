---
name: retro-fleet
description: |
  Fleet-aware engineering retrospective stitched across every Mac in the
  mind-meld fleet. Reads the synced mm-events log to dedup commits across
  machines (canonical remote URL + sha) and produce a glanceable, paste-
  able markdown retro mirroring the gstack /retro shape. Output is
  aggregate-accurate, not per-machine.

  Use when asked to "retro across machines", "fleet retro", "how active
  has the fleet been", or "what shipped this week (across all my Macs)".
  Voice trigger aliases: "retro fleet", "fleet activity", "weekly retro
  across machines".
triggers:
  - retro fleet
  - fleet retro
  - retro across machines
  - what shipped this week across machines
  - cross-machine retro
allowed-tools:
  - Bash
---

# /retro-fleet

Fleet-wide engineering retrospective for users of [mind-meld](https://github.com/kbitz/mind-meld).
Stitches activity from every Mac in the fleet into one accurate picture, and
hands you a screenshot-quality ASCII card up front for sharing.

## How it works

mind-meld's `_run_events_tail` writes per-device daily JSONL files at
`~/.local/share/mind-meld/events/<device>-<YYYY-MM-DD>.jsonl` on every push.
Four event types drive the retro: `mm-push`, `git-snapshot`,
`sessions-snapshot`, and `host-usage-snapshot`. Files sync fleet-wide via the
`mm-events` source.

This skill orchestrates a **two-pass flow**:

1. **Pass 1** — call the aggregator. It dedups commits, sums usage, and renders
   the markdown body. The bottom carries two JSON blocks: `MM_THEMES_PROMPT`
   (raw material for the card's NOTEWORTHY line + 3 TOP WORK themes) and
   `MM_HEALTH` (every data-quality issue, with remedies).
2. **Pass 2** — call the aggregator again with `--theme` / `--noteworthy` /
   `--name`. Python re-renders with a pixel-aligned ASCII card at the top.

This split is load-bearing. LLM-padded right borders drift by a char or two
often enough to look janky in screenshots; routing the card through Python's
deterministic padding solves it without making the card content dumber.

## One table, every agent

Claude, Codex and Grok are reported **identically**: fleet-summed tokens,
active days, contributing machines, estimated cost, top model. Both the card's
`AGENTS` block and the body's `## Agents` table use that one shape.

The card counts currently registered machines with observed usage; each row's
`Machines` count includes all observed contributors. Historical Claude usage
can remain after deregistration, explained by `unregistered_devices` in
`MM_HEALTH`. If the registry is unavailable, the card counts all observed
machines and omits the denominator.

Comparing the rows is the point. They share a unit (tokens), a window, and a
counter basis (input + cache write + cache read + output, disjoint counters
only). Say "Claude carried roughly twice Codex's volume" if the numbers say so.

Four things are still true and still matter:

1. **Days are a lower bound.** A machine that never pushed in the window
   contributes no days, and a peer on an older mm publishes a coarser shape.
   The error is one-directional: it can only understate.
2. **`≥` is a floor, `~` is an estimate, `—` is unavailable — never zero.**
   Every dollar cell carries one. `cost_floor` explains a priced `≥` subtotal;
   `cost_unavailable` explains a visible row with no priced basis. Legacy
   inclusive counters use `legacy_counters`, with affected machines and an
   upgrade remedy, and never carry numeric or extrapolated pricing claims.
3. **Sources still differ.** Claude's tokens come from Claude Code session
   logs; Codex and Grok come from each machine's local agent ledger. Both are
   fleet sums; both can double-count a migrated home directory. The detector
   below flags possible host-ledger overlap; it does not prove that Claude Code
   session histories are disjoint.
4. **An absent row means unobserved, not zero.** Check `MM_HEALTH`.

A partial or failed reader makes every agent's priced total conservative
(`≥`): the wire cannot identify which model families that reader omitted.
Do not guess ownership from the reader's name. Grok's inherent prompt-size
pricing uncertainty remains specific to its models. Missing or rejected price
detail names its machine and the acceptor's `detail_reason` when available.
Repeated hostnames carry short device IDs so remedies identify the right Mac.

### The duplicate-ledger detector

Pre-1.1 the renderer summed Claude's tokens fleet-wide but refused to sum
Codex's or Grok's, on the theory that a migrated home directory yields two
device ids with overlapping history and nothing could detect the overlap. The
asymmetry was indefensible — Claude carried the identical hazard and was summed
anyway — and the premise was false. A migration copies the ledger byte for
byte, so duplicated days carry *identical* counter tuples on both devices.

`_detect_duplicate_ledgers` looks for that signal. If it fires, `MM_HEALTH`
reports code `duplicate_ledger` and names the affected agents; their token and
cost figures may be double-counted. Equal aggregate counters can also occur
independently: inspect `mm devices` and `--dump-host-usage` before recommending
device removal. Day counts are unaffected — a set union is idempotent.

## Step 0: preflight

Within Step 0, only stage 0A can stop the run: 0B is informational and must not
change what you do next. This rule scopes to Step 0 only — a failed or
malformed `mm retro-fleet` in Step 2 is still fatal; never synthesize a card
from output you did not get. On a healthy machine Step 0 is silent — do not
narrate the preflight.

**0A — is `mm` on PATH and working?** Its own block, its own contract.

```bash
command -v mm
```

If that exits non-zero, **STOP.** Do not run Steps 1-5. Tell the user:

> `mm` is not on your PATH, so I stopped before running the retro. Check
> `pipx list | grep mind-meld`. If it is listed there, this is a PATH-order
> problem, not a missing install. After you repair it,
> restart the agent so it reloads SKILL.md

If `mm` resolves, run:

```bash
mm --version
```

If that fails, **STOP.** A broken install is not a degraded run. Quote the
error, tell the user to repair it, and restart the agent.

Require **mm v1.1.0 or newer** before continuing. Below that floor, STOP: the
aggregator emits the pre-1.1 sections and no `MM_HEALTH` block, so this file's
instructions describe surfaces its output does not have. Tell the user to
upgrade, verify `mm --version`, run `mm install-skills`, and restart the agent
so it reloads this skill — a store refresh cannot change instructions already
loaded in a session. Do not infer a fresh capture from exit 0; verify the
recorded timestamp and publication with `mm status`.

Peers may lag the rendering Mac. A machine needs **mm v0.14.17 or newer**
for attended capture publication. Peer-binary upgrades needed for host capture
target this floor; local pricing upgrades and diagnostics have their own remedies.

**0B — after Step 1, relay an upgrade notice if one appeared.** Step 1 runs
interactive `mm push`, whose tail may print something about upgrading. If it
did, repeat it verbatim, then add: this retro may omit blocks added after your
installed version; absent means unmeasured, not zero. The upgrade command is
`pipx install --force git+https://github.com/kbitz/mind-meld.git@latest` — do
not invent a different one. After they run it they still need
`mm install-skills`, then an agent restart.

Silence is not evidence of freshness. The notice is 24h-throttled, skipped when
`[upgrade] auto_check = false` or `--no-check-version`, and network-dependent.

## Step 1: refresh fleet state

Push first, then pull. Attended push refreshes host usage even without user
content changes; Git/session activity capture still requires a substantive
push. Use `mm recapture 30d` for omitted Git history, not repeated empty
pushes.

Use `mm push`, not `mm autopush`. The quiet autopush path gets the 250ms walk
budget instead of 500ms AND takes `_decide_token_walk_policy`'s cold-cache
branch, which drops both `tokens_by_day` and `skills_by_day` for every project
— so refreshing through it made the retro's own refresh the most
truncation-prone push in the system, immediately before the retro read that
snapshot.

The two commands do NOT have the same failure contract, so don't treat a
non-zero exit as fatal. `mm autopull` exits 0 on every error path. `mm push`
exits 1 on missing config, an unavailable passphrase, or a lock held by a
concurrent autopush hook — all routine. Run both, ignore a non-zero exit from
`mm push`, and continue to Step 2 with whatever state exists on disk.

```bash
mm push
mm autopull
```

Skip Step 1 only if the user explicitly asks for a "stale" or "offline" retro,
or if they just ran `mm push` and `mm pull` themselves. Step 0 still runs.

## Step 2: first-pass aggregation

```bash
mm retro-fleet <window>
```

Substitute `<window>` with what the user asked for (`7d`, `30d`, `90d` — days
only). Default is `7d`.

Do NOT fall back to `python -m mind_meld.skills.retro_fleet.aggregator`: on
most macOS systems `python` is not on PATH, and pipx-installed mm lives in an
isolated venv that nothing outside it can import.

## Step 3: synthesize themes + noteworthy

The first-pass output ends with `<!-- MM_THEMES_PROMPT -->`. Read it. It
carries top repos, the ship-of-the-window commit, window stats, the
commit-type mix, burst shape, skill counts, and the per-agent rollup.
Skill entries carry bounded display names and invocation counts. Agent tokens
are `null` when `counters_known` is false; never infer or quote a token count
for those rows.

- **NOTEWORTHY** — one line, ≤55 chars. The single biggest thing shipped. Lead
  with the verb; name the artifact, not the commit. "Shipped fleet-wide skill
  counts (mm v1.1.0)" not "v1.1.0 fix(retro): track skills_by_day".
- **TOP WORK** — three bullets, each ≤55 chars. Themes, not individual
  commits. Lead with the verb. No leading dashes — Python adds the glyph.

Python truncates with `…` when content overflows; aim short on purpose.

## Step 4: second-pass card render

```bash
mm retro-fleet <window> \
  --name <handle> \
  --noteworthy "<your noteworthy line>" \
  --theme "<theme 1>" \
  --theme "<theme 2>" \
  --theme "<theme 3>"
```

Derive `--name` from `git config --global user.email` when the user hasn't
given one.

**Echo the output as your assistant message text — do NOT rely on the bash
tool result alone.** Claude Code collapses bash output behind Ctrl-O, so just
running the command leaves the card buried. Paste stdout into your reply in
two pieces:

1. The ASCII card (lines from `╔═══╗` through `╚═══╝` inclusive) inside a
   fenced ` ```text ` block. The card uses box-drawing chars and space padding,
   which markdown collapses outside a fence.
2. The markdown body that follows pastes inline, unwrapped.

Then continue with Step 5 in the same message.

## Step 5: health, then narrative

### Health — use judgment

The body ends with at most one italic line: `_Data health: N items worth
knowing about — ask me to diagnose._` The detail is in the `MM_HEALTH` JSON
block on the first pass. **Read it and decide what matters.** This is the whole
reason the retro is an LLM skill and not a program: the aggregator used to
render every caveat it knew, and the result was a wall of hedging longer than
the data. The current health-code interface preserves diagnostic meaning and
remedies; it does not preserve every old Notes sentence verbatim.

Your job:

- Say **one sentence** about anything that changes how the user should read
  the numbers. A silent machine, a double-counted ledger, a floor on a headline
  cost — those matter.
- Say **nothing** about noise. "1 of 16 pushes captured 0 repositories" does
  not change a 172-commit picture. "8 of 16" does.
- Then offer: *"Want me to diagnose?"* If they say yes, walk the `MM_HEALTH`
  entries, run the `remedy` commands where they're local, and report back.
- Never invent a cause. Each entry carries `code`, `detail`, and usually
  `remedy`; the wire does not always say *why* something failed, and an
  unrecognized `code` is reported verbatim, never interpreted.

Known codes: `fleet_incomplete`, `registry_unavailable`, `duplicate_ledger`,
`legacy_counters`, `cost_floor`, `cost_unavailable`, `sessions_incomplete`, `tokens_incomplete`,
`pricing_extrapolated`, `skills_incomplete`, `unpriced_models`,
`agent_coverage`, `unregistered_devices`, `discovery_errors`, `git_budget`,
`git_gap`, `zero_repo_capture`, `parse_errors`, `window_exceeds_retention`,
`fleet_changed`.

Two of these change what a number *means* and should almost always surface:
`duplicate_ledger` (possible double-counted tokens and cost for the named agents) and
`zero_repo_capture` (the commit count is a lower bound — do not compute a trend
from it, and do not write the narrative off it).

### Narrative

Append three short paragraphs in the chat, NOT to the card:

- **Praise (one specific thing).** Anchor in actual commits or stats. Not
  "great work" — say exactly what was good. "Six commits restructured the
  lockedjson contract without breaking the flock contention semantics — that's
  textbook refactor discipline."
- **Level-up (one specific thing).** Frame as investment, not criticism. "Test
  ratio held at ~25% this window; lifting it past 40% before the next major
  refactor would cushion regressions."
- **Focus next window (one specific thing).** Forward-looking and actionable.

Match the tone: specific, earned, no coddling. Praise should feel like
something you'd actually say in a 1:1. Skip generic compliments. If the data
doesn't support a confident take, say so and skip the section rather than
fluffing it.

Anchor praise in things the user controls. Cache hit ratio is not one of them —
it was removed from the output in 1.1 for that reason.

## Trends vs prior Nd

For windows shorter than 14d the body includes a `## Trends vs prior <N>d
(A → B)` two-column table (commits, lines added, lines removed, active days)
computed against the immediately preceding equal-length window. Identical in
both passes. Windows of 14d and longer omit it — week-over-week already owns
period-over-period there.

This is NOT a delta vs the last time the command was run. Do not add a trends
line to the ASCII card: the card is width-constrained and a down-arrow on a
shareable artifact is public self-flagellation.

If the section is missing on a 7d retro whose current window has commits, the
heading is still present with an `_Unavailable: ..._` italic line — coverage is
incomplete, not "no change". Never invent a trend from the two windows.

## Author email filtering

By default the aggregator filters commits to those authored by `git config
--global user.email` plus any `[retro].author_emails` aliases in the user's mm
config.toml. To render ALL fleet commits:

```bash
mm retro-fleet <window> --no-author-filter
```

## Raw host inventory

For debugging a host row, dump the accepted per-machine inventory:

```bash
mm retro-fleet <window> --dump-host-usage
```

Hidden flag, JSON on stdout, no card or body. It exposes the complete retained
per-model host inventory by machine; health entries carry selected diagnostics.
Do not narrate raw per-model host data as actual spend, market share, or a
cross-machine total — the `## Agents` table is the fleet view, and this dump is
per machine precisely so you can see which one is misbehaving.

## Custom events directory

```bash
MM_EVENTS_DIR=/path/to/events mm retro-fleet <window>
```

The aggregator's default is `~/.local/share/mind-meld/events/`.

## What this skill does NOT do

- **It does not query GitHub.** Everything comes from the synced events log.
  The PR count is detected from commit subjects, not verified merge status.
- **It does not report per-machine spend.** Machines appear as a count and, in
  health entries, by hostname. The pre-1.1 per-machine dollar tables were
  removed: they keyed on unreadable 8-hex ids and invited exactly the
  cross-machine summation they warned against. Machine-level forensics belong
  in `mm diag`.
- **It does not include sessions from peers on pre-v0.11.0 mm.** `MM_HEALTH`
  names them under `sessions_incomplete`.
- **It does not save output to a file.** `> /tmp/retro.md` is the save story.
- **It does not read `active days` as sessions, prompts, hours, or intensity.**
  It is a count of distinct UTC days on which that agent moved a counter.
