# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

## Unprocessed

- [retro-fleet] **Token-usage measurement under Claude Code activity.** User
  feedback on v0.11.10 retro: "would love to find a way to measure token
  usage here." Source data is per-message `.message.usage` in every session
  jsonl (input_tokens, cache_creation_input_tokens, cache_read_input_tokens,
  output_tokens). Implementation cost is non-trivial: (1) bump
  `EVENTS_SCHEMA_VERSION` to 3 with new `tokens_*` fields on
  `SessionMetadata`, (2) walk each project's jsonls line-by-line summing
  usage from assistant messages, (3) add a per-jsonl sidecar cache
  (`<jsonl>.tokens.txt`, mtime-keyed) so each push only re-reads files
  that changed — without it, `_run_events_tail`'s 250ms autopush budget
  blows on busy fleets, (4) extend `aggregate_sessions` to surface
  per-window totals, (5) render in Claude Code activity section.
  Mixed-fleet rule: pre-v0.11.13 peers emit v=2 snapshots without tokens;
  count them as "tokens incomplete: N peer(s) on pre-v0.11.13" in the
  Notes section, mirror of the existing v=1 sessions handling.
