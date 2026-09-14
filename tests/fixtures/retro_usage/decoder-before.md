

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
- `N tokens from N unpriced model(s) excluded from cost estimate: <ids>.` —
  those models contribute to the token total but not the cost line. The ids
  are sanitized, sorted, and capped. Do not invent a rate for a named id.
- Not available for `<device>`: that Mac runs an mm that reported token
  counters in an older format. Run `pipx upgrade mind-meld` and `mm push`
  there, then re-run. — the economics row (and that machine's token columns)
  show `—`, never a number. Inclusive counters would be a ceiling up to ~2x
  high; do not treat `—` as zero and do not estimate the missing dollars.
- API list-rate equivalent unavailable for `<device>`: its agent-log
  snapshot predates this window. Run `mm push` on that Mac, then re-run. —
  no observation exists inside the requested window, so the economics row
  shows `—`, never a confident `$0`.
- API list-rate equivalent for `<device>` is a floor (>=): <causes>. —
  the marker is binary; the causes name which of: unpriced models, a host
  that declared totals incomplete, a dropped reader, tokens the per-day
  model cap left unattributed, or an unreconstructable long-context tier.
  An all-unpriced device renders `>=$0.00`, not
  unavailable: zero is the priced subtotal and the named tokens sit above
  that floor. Report the named cause. Do not sum the per-machine figures.
- Grok's logs do not record per-request prompt sizes; no action resolves this.
  — an inherent floor cause, distinct from actionable reader or pricing gaps.
  The at-most figure bounds that model's recorded token charges only: never
  average it with the floor, never sum machines, never present it as the
  machine's cost. Server-side tool fees are excluded. An incomplete snapshot
  or nonzero model cache writes suppress the at-most figure entirely.
- Unpriced model(s): upgrading mm on the machine that renders this report may
  price it; republishing does not add a rate; do not estimate. Name the ids and
  the rendering-machine remedy; do not tell the producing Mac to republish
  merely to add a price.
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

