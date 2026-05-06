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
Stitches activity from every Mac in the fleet into one accurate picture.

## How it works

mind-meld's `_run_events_tail` writes per-device daily JSONL files at
`~/.local/share/mind-meld/events/<device>-<YYYY-MM-DD>.jsonl` on every push.
Three event types: `mm-push`, `git-snapshot`, `sessions-snapshot`. Files
sync fleet-wide via the `mm-events` source.

This skill runs the aggregator that ships with mm
(`mind_meld.skills.retro_fleet.aggregator`) to read those files, dedup commits
by `(canonical_remote_url, sha)`, sum sessions across `(device, source_root, claude_dir)`
tuples (latest-snapshot-wins per tuple — the v=2 schema is full inventory),
and render a markdown retro.

## Step 1: invoke the aggregator

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

## Step 2: present the output

The aggregator writes complete markdown to stdout. Show it to the user
verbatim. The output is paste-ready for iMessage, Slack, or email — no
post-processing required.

The aggregator's output may include a `## Notes` section at the end
consolidating any of these data-quality / diagnostic lines (the section is
omitted when there is nothing to surface):

- `Fleet incomplete: N registered device(s) haven't pushed events in this
  window.` — activity may be incomplete from those peers.
- `N unregistered device id(s) had events in this window (filtered out).` —
  phantom event files from de-registered or test-leaked devices were
  skipped from the rendered count. Stale files reap automatically after 90
  days via `mm gc`.
- `Sessions count incomplete: N peer(s) on pre-v0.11.0` — those peers still
  emit v=1 sessions snapshots (delta semantics). Their session totals are
  honestly omitted instead of double-counted.
- `N event(s) skipped due to parse errors in mm event log.` — torn JSONL
  lines were skipped. Output is partial.
- `Requested Nd window exceeds the 90-day events retention.` — user asked
  for a window longer than `EVENTS_RETENTION_DAYS`. Older days are reaped
  by `mm gc` and not in the data.

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
- It does not aggregate `~/.gstack/analytics/` data across the fleet. Skill
  invocation counts are read locally only — the section is labeled "this
  machine only".
- It does not currently measure Claude Code token usage. Adding it is a
  known follow-up — would require summing `message.usage` from each session
  jsonl into a v=3 sessions-snapshot field, with a per-file sidecar cache
  to stay under the events-tail wall-clock budget.
- It does not save the output to a file. `> /tmp/retro.md` is the v1 save
  story. A `--save` flag is deferred to v2.
