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
Three event types drive the rendered retro: `mm-push`, `git-snapshot`,
`sessions-snapshot`. A fourth type, `host-usage-snapshot`, is accepted as
last-known-good host inventory per device (Track 22A) and is **not**
rendered as window spend. Files sync fleet-wide via the `mm-events` source.

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

The `MODELS` block is also a **second-pass share-card feature**. The first
pass remains useful raw markdown and deliberately has no card. Its rows group
observed model IDs from Claude Code session snapshots; they do not claim
fleet-host or vendor adoption, and an omitted family means unobserved rather
than zero. The header states that source coverage directly
(`MODELS (Claude Code sessions)`) and the block points to Notes when token
coverage is incomplete.

The `AGENT LOGS` block (v0.12.37) is the second-pass sibling for **other**
coding agents — Codex, Grok, OpenCode. It reports **rhythm, never magnitude**:

```text
AGENT LOGS (1 of 3 machines with agent activity)
Codex models: seen on 5 days
```

Read it as: *"the Codex model family appeared on 5 distinct UTC days in this
window, and 1 of 3 known machines contributed any agent activity."* Token
totals for these agents live in the body's `## Agent activity` table, per
machine, and nowhere on the card.

Three things this block is not, each of which the data genuinely cannot
support:

1. **A row is a model family, not an agent.** The wire carries no
   reader-to-family attribution at all, and model IDs are bucketed by prefix,
   so the Codex and OpenCode readers both land GPT models in the `Codex
   models` family. `Claude (via agents)` is a legal row — that is OpenCode
   running a Claude model — and it means something different from the `MODELS`
   block's `Claude` row.
2. **`seen on N days` is a lower bound, not a count.** Track 32A made every
   reader per-turn, so resuming a session no longer moves its total onto a
   later day. What remains: a peer on an older mm still publishes the old
   shape, and a machine that never pushed in a window contributes no days.
   It can only ever understate.
3. **An absent or empty block is not zero.** The body always names the cause
   (no snapshot yet, no reader contributed, all snapshots stale, nothing active)
   with a remedy. An empty contributor list can mean no source is enabled or
   that every selected reader had no attributable local ledger; read the note
   instead of inferring which one occurred.

## Step 0: preflight

Within Step 0, only stage 0A can stop the run: 0B is informational and
must not change what you do next. This rule scopes to Step 0 only — it says
nothing about Steps 1-5, which keep their own failure contracts. In
particular, a failed or malformed `mm retro-fleet` in Step 2 is still fatal;
never synthesize a card from output you did not get. On a healthy machine
Step 0 is silent — do not narrate the preflight.

**0A — is `mm` on PATH and working?** Its own block, its own contract.
Do not fold this into Step 1's `mm push` / `mm autopull` block.

```bash
command -v mm
```

If that exits non-zero, **STOP.** Do not run Steps 1-5. Anything you
produced would be missing this machine's activity entirely. Tell the user:

> `mm` is not on your PATH, so I stopped before running the retro. Check
> `pipx list | grep mind-meld`. If it is listed there, this is a
> PATH-order problem, not a missing install. After you repair it, restart
> the agent so it reloads SKILL.md.

If `mm` resolves, run:

```bash
mm --version
```

If that fails, **STOP.** A broken install is not a degraded run; later
`mm` commands cannot work either. Quote the error, tell the user to
repair it (same `pipx list` check), and restart the agent so it reloads
SKILL.md.

This proves shell resolution and that the binary starts. It does not
prove the install is current.

**0B — after Step 1, relay an upgrade notice if one appeared.** Step 1
runs interactive `mm push`, whose tail may print something about
upgrading (GitHub `/tags` check). If `mm push` printed anything about
upgrading, repeat it verbatim in your reply, then add: this retro may
omit blocks added after your installed version; absent means unmeasured,
not zero. The upgrade command is
`pipx install --force git+https://github.com/kbitz/mind-meld.git@latest`
— do not invent a different one. After they run it, they still need
`mm install-skills`, then restart the agent so it reloads SKILL.md.

Silence is not evidence of freshness. The notice is 24h-throttled,
skipped when `[upgrade] auto_check = false` or `--no-check-version`, and
network-dependent.

## Step 1: refresh fleet state

Push first, then pull. `_run_events_tail` only fires on push, so today's
local commits, session tokens, and skill counts aren't in the events JSONL
until `mm push` writes them — without this, the retro is missing the
running machine's most recent activity. `mm autopull` then collects what
other Macs have pushed since the last sync.

Use `mm push`, not `mm autopush` (v0.12.16). The quiet autopush path gets
the 250ms walk budget instead of 500ms AND takes
`_decide_token_walk_policy`'s cold-cache branch, which passes
`token_cache_files=None` and drops both `tokens_by_day` and `skills_by_day`
for every project — so refreshing through it made the retro's own refresh
the most truncation-prone push in the system, immediately before the retro
read that snapshot. `mm push` is safe here: `_maybe_prompt_migration`
short-circuits to a stderr warning on a non-TTY, which is what a skill Bash
call is.

The two commands do NOT have the same failure contract, so don't treat a
non-zero exit as fatal here. `mm autopull` exits 0 on every error path,
including when mm isn't initialized. `mm push` exits 1 on missing config, an
unavailable passphrase, or a lock held by a concurrent autopush hook — all
routine, none of them a reason to abandon the retro. Run both, ignore a
non-zero exit from `mm push`, and continue to Step 2 with whatever state
exists on disk.

```bash
mm push
mm autopull
```

Skip Step 1 only if the user explicitly asks for a "stale" or "offline"
retro, or if they just ran `mm push` and `mm pull` themselves. Step 0
still runs.

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

Call the aggregator again with the synthesized strings and `--name` set to
the user's identifier (use `git config --global user.email` to derive a
short handle when the user hasn't said one explicitly). `--no-save` is a
no-op as of v0.12.39 (kept so this invocation stays compatible across an
upgrade whose skill store still has the old SKILL.md).

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

The card's `N detected GitHub PR references` line is global delivery context,
not a model-family metric or verified merge status. It appears once above
`MODELS`, including when the count is zero. When the card says
`Model-token coverage incomplete: N peer(s); see Notes`, treat the family rows
as a partial subtotal: the Notes identify affected peers and tell you to run
`mm push`, then upgrade if the warning persists.

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
  actionable. "Land the `--format json` export so a weekly cron can
  keep a long-horizon archive now that snapshot files are gone."

Match the **tone block**: specific, earned, no coddling. Praise should
feel like something you'd actually say in a 1:1; growth suggestions
should feel like investment advice. Skip generic compliments. If the
data doesn't support a confident take, say so and skip the section
rather than fluffing.

## Notes section in aggregator output

The body's `## Notes` section consolidates these data-quality lines (the
section is omitted when there is nothing to surface). This list is closed
and 1:1 with strings the aggregator emits. **An unlisted `## Notes` line
is reported verbatim and never interpreted** — a stale installed copy of
this file must not invent a meaning on a surface whose purpose is not
lying.

Known lines:

- `Fleet incomplete: N registered device(s) haven't pushed events in this
  window.` — activity may be incomplete from those peers.
- `N unregistered device id(s) had events in this window (filtered out).` —
  phantom event files from de-registered or test-leaked devices were
  skipped from the rendered count. Stale files reap automatically after 90
  days via `mm gc`.
- `Sessions count incomplete: N peer(s) on pre-v0.11.0` — those peers still
  emit v=1 sessions snapshots (delta semantics). Their session totals are
  honestly omitted instead of double-counted.
- `Tokens incomplete on <peers>: pre-v0.11.0 session schema + pre-v0.11.14 OR cold token cache` —
  those peers cannot provide complete model-token totals. The reasons join with
  ` + ` and name `pre-v0.11.0 session schema` and/or `pre-v0.11.14 OR cold token
  cache`. Run `mm push` on the named peers to rebuild the cache, then upgrade
  if the warning persists.
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
- `No agent-log snapshots yet from N machine(s) — run mm push there…` —
  no accepted host-usage snapshot on those machines. Unknown, not zero.
- `No agent-log snapshots were accepted from any machine — run mm push on
  each Mac…` — the device registry was unavailable so missing-device
  detection could not run; still unknown, not zero.
- `No agent-log reader contributed on any machine…` — the row's contributor
  list is empty. That can mean no source is enabled, or that each selected
  reader had no attributable local ledger; it cannot distinguish the two.
  Enable one of those sources if needed, then run `mm push`; do not report a
  consent failure as a fact.
- `No agent activity observed in this window. Counts are lower bounds…` —
  readers ran and found nothing dated inside the window. The bound is
  because a machine that has not pushed contributes no days, and a peer on
  an older mm still reports last-touch totals rather than per-turn ones.
  Report it as observed-nothing, not as zero usage.
- `Agent-log snapshots all predate this window — run mm push…` — every
  accepted snapshot is older than the window, so no current rhythm exists.
- `N machine(s) have no agent-log snapshot (unknown, not zero)…` — those
  machines have not published one yet (pre-v0.12.32, or no push since).
  Never fill the gap with a zero.
- `Agent-log snapshots from N machine(s) were rejected (<reasons>)…` — those
  machines' rows failed validation, usually a version mismatch. Counts
  **machines**, not rows, so one broken writer cannot inflate it.
- `Known-fleet count unavailable (`mm devices --format=json` failed).` — the
  header drops the "of M known" tail. Not a data-loss signal.
- `N tokens from N unpriced model(s) excluded from cost estimate.` — those
  models contribute to the token total but not the cost line.
- `N discovery error(s) recorded — run mm diag.` —
  forensic; do not invent a cause. Those notices go to an unattended hook's
  stderr and are persisted nowhere.
- `Machine X captured 0 repositories on N of M pushes; its commits are
  missing from this window.` — that machine's git-snapshot rows in the
  window have `projects == []`. The commit count is a **lower bound**.
  Name the machine. Do not compute a trend from the commit number. Do not
  write the Step 5 narrative off it.
- `N record(s) skipped due to parse errors. Output may be incomplete.` —
  foreign-caller fallback; treat like the mm-event parse-error line.
- `Fleet composition changed between windows:` — the set of devices that
  pushed in the prior Nd differs from this Nd. Report it; never compute the
  trend yourself from the two windows.
- `Host-usage reader(s) <readers> failed on the latest push from <machines>
  — on <machine>, run mm diag and inspect host_usage.<reader>.` — that
  machine's host reader ran and failed. Name the machine and the reader.
  Do not treat the missing host as zero. Do not promise that `mm push`
  repairs it; the wire carries which reader failed, not why. Walk over to
  the named machine if you are not already on it.
- `Host-usage totals from <readers> on <machines> are incomplete (the host
  declared those totals incomplete) — on <machine>, run mm diag and inspect
  host_usage.<reader>.` — the source contributed usable totals, but the
  host explicitly declared those totals incomplete. Treat the number as a
  floor, not an estimate. Same remedy as the failed-reader line: `mm diag`
  on the named machine, never a bare `mm push`.
- `Git walk ran out of budget on <machines> — some repositories were not
  captured. On those machines, run mm diag and inspect
  git_capture.recorded.walk_budget_aborts; this is not a missing push.` — the capture
  ran and exhausted its budget. Different from a gap. Do not tell the user
  to `mm recapture` as if nothing was attempted.
- `Git history has an uncovered interval on <machines> — those windows were
  never captured. On those machines, run mm recapture for the missing
  window, then mm diag to confirm.` — a device with no `git_capture` at all
  is unknown, not a gap; do not invent one. A recapture row covers its
  interval even though it is not a push.

## Trends vs prior Nd

For windows shorter than 14d the body includes a `## Trends vs prior <N>d
(A → B)` two-column table (commits, lines added, lines removed, active
days) computed from the synced events corpus against the immediately
preceding equal-length window. Identical in both passes. Windows of 14d
and longer omit this section — week-over-week already owns
period-over-period there.

This is NOT a delta vs the last time the command was run. Do not add a
trends line to the ASCII card: the card is width-constrained and a
down-arrow on a shareable artifact is public self-flagellation.

If the section is missing on a 7d retro whose current window has commits,
the heading is still present with an `_Unavailable: ..._` italic line —
coverage is incomplete, not "no change". Never invent a trend from the
two windows yourself.

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

- **A commit count is a lower bound.** If Notes reports zero-repository
  captures, say the number is incomplete, name the machine, do not compute
  a trend from it, and do not write the Step 5 narrative off it. A
  `61 -> 4` Trends collapse next to a zero-repository note is missing
  capture, not a bad week.
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
- **It does not compare `MODELS` to `AGENT LOGS`.** Never infer relative
  usage, share, spend, cost, productivity, adoption, or "dominance" between
  them. They differ in source (Claude Code session snapshots vs local agent
  logs), in unit (tokens vs distinct days), in time semantics (a window sum vs
  a lower-bound day count), and in completeness. Allowed: *"Codex-family
  models appeared on five UTC activity days."* Not allowed: *"Claude did most
  of the work"*, or any ratio between the two blocks. Also never read active
  days as sessions, prompts, hours, or intensity.
- **It does not attribute agent-log activity to a specific agent.** The rows
  are model families. The body table names which readers ran, per machine;
  that is the only reader-level claim the data supports. Per-model host
  totals exist on the wire as of v0.12.49 and reach
  `mm retro-fleet --dump-host-usage`, **not the card**. The card stays
  family-only until Group 36. Do not narrate per-model host data as spend,
  market share, or a cross-machine total.
- **It does not treat a missing `AGENT LOGS` block as zero activity.** The
  block is omitted only when no snapshot has ever been accepted, and the body
  always names the cause. A stale `mm` also produces no block — Step 0
  already stopped you if the install is missing or broken; treat any
  remaining absence as unmeasured, not zero.
