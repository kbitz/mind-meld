# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

## Unprocessed

- **[plan-eng-review 2026-04-23 Track 1A]** Full `quiet`-path audit in `cli.py`. Classify every `if not quiet:` gate as "verbose-only" vs "load-bearing signal." Track 1A patches two known load-bearing gates (`_pull_core:1445` corrupt peer manifest, `_push_core:1297` sidecar write failure). The pattern is likely wider. _src/mind_meld/cli.py, ~60 lines._ (S)
