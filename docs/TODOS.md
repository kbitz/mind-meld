# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

Single source of truth — there is no root-level `TODOS.md`. The two files were
reconciled on 2026-08-14; the root file's live inbox won and moved here, and the
`## Inbox` heading was renamed to `## Unprocessed` (what `/roadmap` drains).

## Unprocessed

- **[23A] `## Trends vs last retro` never reaches the shareable output (BUG, P2).**
  `main` renders the card iff `has_card_input` and saves a snapshot iff **not**
  `has_card_input` (and SKILL.md's second pass also passes `--no-save`), so the
  second pass compares against the snapshot the first pass wrote seconds earlier
  over the identical corpus: every delta is zero and the section is skipped.
  Verified by faking a differing prior snapshot, at which point pass 2 *does*
  render deltas — so the mechanism works and only the baseline is wrong. Net
  effect: the trends feature is invisible in the output the user actually keeps,
  and both README and SKILL.md described it as if it appears on repeat runs (both
  now carry the caveat). Out of 23A's scope because a fix changes save/load
  semantics for all seven existing metrics, not just host data. Options: let the
  second pass load the snapshot *before* the first pass's, carry the computed
  delta between passes, or make the first pass emit it for the skill to paste.
- **[23A] Deterministic demo/fixture path for `retro-fleet` (P2).** Fresh-clone
  time-to-first-output is 10-30 minutes and nondeterministic: it needs a 3.11+
  interpreter, a venv, an editable dev install, `mm init`, an enabled host
  source, a substantive `mm push`, and then two aggregator passes — and the
  `AGENT LOGS` block only appears if accepted, non-stale, positive in-window data
  exists. For validating a *renderer* that is absurd. Something like
  `mm retro-fleet --demo` over a bundled synthetic corpus would make the card
  reproducible in three commands. The 295-test suite is the current deterministic
  path and is adequate for CI, so this is developer ergonomics, not correctness.
- **[23A] Un-hide and rename `--dump-host-usage` (P3).** Documented only in
  `CHANGELOG.md` and hidden from `mm retro-fleet --help`, so the primary forensic
  hatch is undiscoverable. Its name also now clashes with the reader-facing
  vocabulary the card uses (agent logs, machines, readers, model families) and
  "dump host usage" reads like spend when it is accepted retained inventory.
  Prefer `--host-inventory-json` or a `diagnostics --json` subcommand, keeping the
  old flag as an alias. Deferred as a CLI-surface change; the flag shipped in
  v0.12.36.
- **[23A] Accept a bare integer window (P3).** `mm retro-fleet 7` is rejected
  while `7d` works. The skill translates natural language so agents never hit it,
  but direct CLI users do. Either accept `N` as days or improve the error to
  name the fix (`use '7d'`).
- **[23A] `mm status` agent-coverage row (P3).** The retro is weekly; `mm status`
  is the daily signal and currently says nothing about whether agent-log capture
  is working. Outside 23A's blast radius (`cli.py` + the status contract).
- **[23A] Deregister/prune retired devices (P3).** A retired-but-registered Mac
  inflates every "N of M machines" denominator forever, which is the root cause
  the 23A coverage wording had to work around rather than fix. Wants
  `mm devices --prune` or a staleness nudge in `devices.py` / `cli.py`.
- **[23A] Reset-aware per-device snapshot deltas (P3, successor track).** The only
  honest route to real per-agent *window spend*, as opposed to the lower-bound day
  counts 23A ships. Needs ≥2 retained snapshots per device (22A keeps only the
  latest), counter-reset detection, and a new wire/consumer invariant. Explicitly
  a data-layer track, not a renderer change.

Host-parity inbox drained 2026-08-17: Grok allowlist shipped in Track 22B; Grok
skill-link → Track 23B; Codex/Grok sessions-snapshot refuse → Future.

_Last updated 2026-08-18 by Track 23A (/autoplan)._
