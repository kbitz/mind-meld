# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

Single source of truth — there is no root-level `TODOS.md`. The two files were
reconciled on 2026-08-14; the root file's live inbox won and moved here, and the
`## Inbox` heading was renamed to `## Unprocessed` (what `/roadmap` drains).

## Unprocessed

_(empty — drained 2026-08-20 by `/roadmap`.)_

Drain record, 7 items from the 2026-08-18 Track 23A pass:

- 1 placed: the `## Trends vs last retro` bug became Track 24B. The mechanism was
  re-verified at HEAD — v0.12.37 documented the caveat but did not change the
  save/compare circularity, so the bug is still open.
- 1 discharged: the `mm status` agent-coverage row was absorbed into Track 25A,
  which adds a `mm status` line on the same surface under the same one-line
  budget. `mm status` still has zero skill or agent-coverage references, so the
  underlying gap is real — this was a merge, not a deletion.
- 5 deferred to `## Future` in `docs/ROADMAP.md`: demo/fixture path,
  `--dump-host-usage` rename, bare-integer retro window, retired-device pruning,
  and reset-aware snapshot deltas.
- 0 killed.

Host-parity inbox drained 2026-08-17: Grok allowlist shipped in Track 22B;
Codex/Grok sessions-snapshot refuse → Future. The Grok skill-link item routed to
Track 23B, which was dissolved on 2026-08-20 after failing its `/autoplan`
premise gate; its intent now lives in Group 29 behind Groups 24-28.

_Last updated 2026-08-20 by `/roadmap`._
