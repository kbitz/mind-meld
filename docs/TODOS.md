# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

Single source of truth — there is no root-level `TODOS.md`. The two files were
reconciled on 2026-08-14; the root file's live inbox won and moved here, and the
`## Inbox` heading was renamed to `## Unprocessed` (what `/roadmap` drains).

## Unprocessed

- `mm retro-fleet --format json` for scripted metric export. The v0.12.0 snapshot JSON was the de-facto export surface and Track 24B removed it. A weekly `mm retro-fleet 7d --format json >> ~/retro-history.jsonl` is a strictly better long-horizon archive than the snapshot ever was. (24B / DX-6)
- `--no-trends` is a deliberate non-goal. The section self-suppresses when the current window is empty and the point is that it should appear otherwise. Do not add an opt-out flag. (24B / DX-6)
- `_iter_jsonl` in `aggregator.py` uses unbounded text iteration with replacement decoding, unlike the bounded binary readers elsewhere (`token_usage.iter_bounded_lines`). The free second pass for prior-period trends is free because someone already paid to materialise the whole corpus. Follow-up, not a 24B expansion. (24B / Eng F1)

Drain record, 7 items from the 2026-08-18 Track 23A pass:

- 1 placed: the `## Trends vs last retro` bug became Track 24B (2026-08-21 regen:
  still 24B, now rooming with 24A in Group 24). The mechanism was re-verified at
  HEAD — v0.12.37 documented the caveat but did not change the save/compare
  circularity, so the bug is still open.
- 1 discharged: the `mm status` agent-coverage row was absorbed into Track 25A
  (2026-08-21 regen: the one-line nag now lives on Track 24A with the store).
- 5 deferred to `docs/roadmap-future.md`: demo/fixture path,
  `--dump-host-usage` rename, bare-integer retro window, retired-device pruning,
  and reset-aware snapshot deltas.
- 0 killed.

Host-parity inbox drained 2026-08-17: Grok allowlist shipped in Track 22B;
Codex/Grok sessions-snapshot refuse → Future. The Grok skill-link item routed to
Track 23B, which was dissolved on 2026-08-20 after failing its `/autoplan`
premise gate; 2026-08-21 regen places it as Track 27A behind Groups 24-26
(Approach B deleted Group 29).

_Last updated 2026-08-21 by `/roadmap`._
