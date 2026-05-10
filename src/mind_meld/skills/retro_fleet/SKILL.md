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
Three event types: `mm-push`, `git-snapshot`, `sessions-snapshot`. Files
sync fleet-wide via the `mm-events` source.

This skill orchestrates a **two-pass flow**:

1. **Pass 1** — call the aggregator. It dedups commits, sums sessions /
   tokens / skills, and renders the markdown body. The bottom of the
   output carries an `MM_THEMES_PROMPT` JSON block: raw material for you
   to synthesize the card's NOTEWORTHY line + 3 TOP WORK themes.
2. **Pass 2** — call the aggregator again with `--theme` / `--noteworthy`
   / `--name` flags. Python re-renders with a pixel-aligned ASCII card
   pinned at the top of the output.

This split is load-bearing. LLM-padded right borders drift by a char or
two often enough to look janky in screenshots; routing the card through
Python's deterministic padding solves it without making the card content
dumber.

## Step 1: refresh fleet state

Push first, then pull. `_run_events_tail` only fires on push, so today's
local commits, session tokens, and skill counts aren't in the events JSONL
until `mm push` writes them — without this, the retro is missing the
running machine's most recent activity. `mm autopull` then collects what
other Macs have pushed since the last sync.

Both commands are silent, never prompt, and exit gracefully on errors or
when mm isn't initialized — safe to run unconditionally.

```bash
mm autopush
mm autopull
```

Skip this step only if the user explicitly asks for a "stale" or "offline"
retro, or if they just ran `mm push` and `mm pull` themselves.

## Step 2: first-pass aggregation

Run this command and capture the output. Substitute `<window>` with what the
user asked for (`7d`, `30d`, `90d`, etc. — days only).

```bash
mm retro-fleet <window>
```

Default window is `7d` if the user did not specify.

If `mm` is not on `$PATH`, the command fails with `command not found`. Do
not retry — surface the error and tell the user to verify their mm install
(`pipx list | grep mind-meld`). Do NOT fall back to
`python -m mind_meld.skills.retro_fleet.aggregator`: on most macOS systems
`python` is not on PATH (only `python3` is), and pipx-installed mm lives in
an isolated venv that nothing outside it can import.

## Step 3: synthesize themes + noteworthy

The first-pass output ends with a fenced JSON block tagged
`<!-- MM_THEMES_PROMPT -->`. Read it. The payload includes top repos by
commit count, the ship-of-the-window commit, and aggregate window stats —
plus the surrounding markdown body shows the commit-type mix, peak hours,
commit bursts, and per-skill counts.

Synthesize:

- **NOTEWORTHY** — one line, ≤55 chars. The single biggest thing
  shipped this window. Lead with the verb; name the artifact, not the
  commit. "Shipped fleet-wide skill counts (mm v0.11.27)" not
  "v0.11.27 fix(retro): track skills_by_day".
- **TOP WORK** — three bullets, each ≤55 chars. Themes, not individual
  commits. Synthesize commit messages into a few cohesive narratives
  (e.g., "Fleet retro polish + token rollup" covers six related
  commits). Lead with the verb. No leading bullets / dashes — Python
  adds the bullet glyph in the card.

Keep them tight. The card has a fixed width and Python truncates with
`…` when content overflows; aim short on purpose.

## Step 4: second-pass card render

Call the aggregator again with the synthesized strings, `--name` set to
the user's identifier (use `git config --global user.email` to derive a
short handle when the user hasn't said one explicitly), and `--no-save`
so the snapshot isn't double-written.

```bash
mm retro-fleet <window> \
  --name <handle> \
  --noteworthy "<your noteworthy line>" \
  --theme "<theme 1>" \
  --theme "<theme 2>" \
  --theme "<theme 3>" \
  --no-save
```

**Echo the output as your assistant message text — do NOT rely on the
bash tool result alone.** Claude Code collapses bash output behind
Ctrl-O, so just running the command leaves the card buried. Paste
stdout directly into your reply, split into two pieces so both render
correctly:

1. The ASCII card (lines from `╔═══╗` through `╚═══╝` inclusive) goes
   inside a fenced code block tagged ` ```text `. The card uses
   box-drawing chars + space padding for alignment, which markdown
   collapses outside a code fence.
2. The markdown body that follows the card pastes inline, unwrapped,
   so headers and lists render normally.

Then continue with Step 5 in the same message. The card is paste-ready
for iMessage / Slack / email; the body is for readers who want the
deeper data.

## Step 5: write the praise / level-up / focus narrative

The card is the shareable artifact. The conversation is where the
narrative lives. After showing the second-pass output, append in the
chat (NOT to the card) three short paragraphs:

- **Praise (one specific thing).** Anchor in actual commits or stats
  from the body. Not "great work" — say exactly what was good. "Six
  commits restructured the lockedjson contract without breaking the
  flock contention semantics — that's textbook refactor discipline."
- **Level-up (one specific thing).** Frame as investment, not
  criticism. "Test ratio held at ~25% this window; lifting it past 40%
  before the next major refactor would cushion regressions."
- **Focus next window (one specific thing).** Forward-looking and
  actionable. "Land the snapshot pruning hook so the retros dir
  doesn't grow unbounded as you settle into weekly retros."

Match the **tone block**: specific, earned, no coddling. Praise should
feel like something you'd actually say in a 1:1; growth suggestions
should feel like investment advice. Skip generic compliments. If the
data doesn't support a confident take, say so and skip the section
rather than fluffing.

## Notes section in aggregator output

The body's `## Notes` section consolidates these data-quality lines (the
section is omitted when there is nothing to surface):

- `Fleet incomplete: N registered device(s) haven't pushed events in this
  window.` — activity may be incomplete from those peers.
- `N unregistered device id(s) had events in this window (filtered out).` —
  phantom event files from de-registered or test-leaked devices were
  skipped from the rendered count. Stale files reap automatically after 90
  days via `mm gc`.
- `Sessions count incomplete: N peer(s) on pre-v0.11.0` — those peers still
  emit v=1 sessions snapshots (delta semantics). Their session totals are
  honestly omitted instead of double-counted.
- `Tokens incomplete: N peer(s) on pre-v0.11.14 OR with cold token cache` —
  those peers' v=2 snapshots omit ``tokens_by_day``. Run `mm push` on the
  named peers to rebuild the cache.
- `Skills incomplete: N peer(s) on pre-v0.11.27 OR with cold token cache` —
  those peers' v=2 snapshots omit ``skills_by_day``. Run `mm push` on the
  named peers (warms the token cache and re-emits the field), or upgrade
  if they're on pre-v0.11.27. *(Distinct from "no skills used this
  window" — empty-dict rows from v0.11.27+ warm-cache peers do NOT
  trigger this.)*
- `N event(s) skipped due to parse errors in mm event log.` — torn JSONL
  lines were skipped. Output is partial.
- `Requested Nd window exceeds the 90-day events retention.` — user asked
  for a window longer than `EVENTS_RETENTION_DAYS`. Older days are reaped
  by `mm gc` and not in the data.

## Trends vs last retro

The aggregator persists a JSON snapshot to
`~/.local/share/mind-meld/retros/YYYY-MM-DD-N.json` after every save-
enabled run. On subsequent runs with the same window, deltas vs the most
recent matching snapshot render as a `## Trends vs last retro` block —
when something changed. No section is rendered for first runs or
zero-delta runs.

## Author email filtering

By default the aggregator filters commits to those authored by `git config
--global user.email` plus any `[retro].author_emails` aliases in the user's
mm config.toml. To render ALL fleet commits without a filter, run:

```bash
mm retro-fleet <window> --no-author-filter
```

## Custom events directory

Power users with a custom `path` on the `mm-events` sync source can override
the aggregator's events directory via env var:

```bash
MM_EVENTS_DIR=/path/to/events mm retro-fleet <window>
```

The aggregator's default is `~/.local/share/mind-meld/events/`.

## What this skill does NOT do

- It does not query GitHub directly. Everything comes from the synced events log.
- It does not include sessions from machines that haven't yet upgraded to mm
  v0.11.0+ (those peers emit pre-v=2 snapshots). The Notes section names
  which peers need to upgrade.
- It does not include skill counts from machines whose latest snapshot
  in the window omits ``skills_by_day`` — either pre-v0.11.27 peers
  (code never emits the field) OR v0.11.27+ peers whose most recent push
  ran with a cold token cache under the autopush gate (skill walk
  skipped, field absent). The Notes section names them; running `mm push`
  interactively on those machines warms the cache and emits the field on
  the next push. Cross-machine skill counts come from each peer's Claude
  Code session jsonls (the same source it walks for tokens) — not from
  gstack analytics.
- It does not save the user-facing output to a file. `> /tmp/retro.md` is
  the v1 save story. A `--save` flag is deferred to v2.
