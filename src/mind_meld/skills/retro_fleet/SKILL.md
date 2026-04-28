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
by `(canonical_remote_url, sha)`, sum sessions across `(device, claude_dir)`
tuples (latest-snapshot-wins per tuple — the v=2 schema is full inventory),
and render a markdown retro.

## Step 1: invoke the aggregator

Run this command and capture the output. Substitute `<window>` with what the
user asked for (`7d`, `30d`, `90d`, etc. — days only).

```bash
python -m mind_meld.skills.retro_fleet.aggregator <window>
```

Default window is `7d` if the user did not specify.

If `mm` is not installed in the venv that's on `$PATH`, the command exits
non-zero with a clear error. Do not retry — surface the error and tell the
user to verify their mm install (`pipx list | grep mind-meld`).

## Step 2: present the output

The aggregator writes complete markdown to stdout. Show it to the user
verbatim. The output is paste-ready for iMessage, Slack, or email — no
post-processing required.

The aggregator's output may include any of these tail breadcrumbs:

- `*Note: N event(s) skipped due to parse errors.*` — visible-failure
  contract: torn JSONL lines were skipped. Output is partial.
- `*Note: requested 365d window exceeds the 90-day events retention.*` —
  user asked for a window longer than `EVENTS_RETENTION_DAYS`. Older days
  are reaped by `mm gc` and not in the data.
- `*Sessions count incomplete: N peer(s) on pre-v0.11.0...*` — those peers
  still emit v=1 sessions snapshots (delta semantics). Their session totals
  are honestly omitted instead of double-counted.
- `*Fleet incomplete: N device(s) haven't pushed events in this window.*` —
  registered devices that haven't pushed during the retro window. Activity
  may be incomplete from them.

## Author email filtering

By default the aggregator filters commits to those authored by `git config
--global user.email` plus any `[retro].author_emails` aliases in the user's
mm config.toml. To render ALL fleet commits without a filter, run:

```bash
python -m mind_meld.skills.retro_fleet.aggregator <window> --no-author-filter
```

## Custom events directory

Power users with a custom `path` on the `mm-events` sync source can override
the aggregator's events directory via env var:

```bash
MM_EVENTS_DIR=/path/to/events python -m mind_meld.skills.retro_fleet.aggregator <window>
```

The aggregator's default is `~/.local/share/mind-meld/events/`.

## What this skill does NOT do

- It does not query GitHub directly. Everything comes from the synced events log.
- It does not include sessions from machines that haven't yet upgraded to mm
  v0.11.0+ (those peers emit pre-v=2 snapshots). The breadcrumb tells the
  user which peers need to upgrade.
- It does not aggregate `~/.gstack/analytics/` data across the fleet. Skill
  invocation counts and eureka moments are read locally only — the section
  is labeled "this machine only".
- It does not save the output to a file. `> /tmp/retro.md` is the v1 save
  story. A `--save` flag is deferred to v2.
