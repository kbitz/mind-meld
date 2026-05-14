# TODOS

Deferred work items. `/roadmap` organizes these into the execution plan.

## Inbox

### [plan-eng-review] Peers we never consciously resolved against can be mtime-skipped by the drain
- **What:** Two related watch-items in the same family. The Track 12A end-of-pull-batch bump (`_bump_canonical_mtime_post_resolve` drain in `_pull_core`) bumps canonical past the keep-local-resolved peers. If that mtime exceeds an unrelated peer's manifest mtime, the next pull from that peer hits the mtime gate and skips instead of re-conflicting. Two trigger surfaces:
  1. **Pre-existing on-disk sidecars** (Codex /plan-eng-review #2, 2026-05-14): a peer whose OLD, still-unresolved `.sync-conflict-*` predates this pull.
  2. **Failed-download peers** (Codex /review P2, 2026-05-14): a peer whose blob download fails this batch (bad blob key, missing blob, decrypt error, path rejection) never reaches `_apply_incoming_file` and never invalidates. Once the transient failure clears on a later pull, that peer's manifest mtime may sit below the bump.
- **Why:** `/plan-eng-review` 2026-05-14 chose NOT to suppress (T4-A) — the sidecar-on-disk is already the durable signal (`mm conflicts` / `mm resolve` still surface it), v0.12.6's resolve-side bump has the identical property, and suppressing would weaken the propagation fix Track 12A exists to deliver. The failed-download case (Codex P2, GATE: PASS) is the same family: bytes still on the peer's manifest, will resolve on next successful pull, just deferred. Both surface as "peer we never consciously resolved against gets mtime-skipped once."
- **Pros of revisiting:** Eliminates narrow accidental-mtime-resolution paths if dogfood shows they actually bite.
- **Cons:** Adds a disk scan to the drain (#1) and per-rel-path failed-download tracking (#2); weakens fleet propagation; precise variants (suppress only for different-peer sidecars; track failed paths per resolved key) are non-trivial code.
- **Context:** Only the pull-time re-conflict / re-download is delayed, never the bytes themselves. Revisit only if dogfood surfaces real cases. Start at the `_pull_core` drain loop + `_find_conflict_files` (#1) or the `_download_and_apply` per-file failure paths (#2). See the deferred-inline subsection in `docs/invariants/conflicts.md`.
- **Depends on / blocked by:** Track 12A landing first (the drain doesn't exist yet).

### [review] Abort transactionality (pre-existing torn-state, surfaced by Track 12A)
- **What:** `typer.Abort()` from the inline `(a)bort` choice propagates out of `_pull_core`'s `try` block. The drain is skipped (Track 12A T2-A intentional). BUT earlier keep-remote / merge writes in the same `_download_and_apply` (and earlier peers within this `_pull_core` invocation) already hit disk — those writes are not rolled back, AND `_pull_one_source` only records outcomes/touched-parents/audit-log AFTER `_download_and_apply` returns, so abort also drops the audit log + fsync for those partial writes. Net: abort can leave changed files plus discarded keep-local decisions plus no audit trail for the partial writes.
- **Why:** Codex adversarial (HIGH 2026-05-14) + Claude adversarial (Finding 5) both flagged. This is **pre-existing** behavior (the abort path has worked this way since the inline prompt shipped) made slightly more asymmetric by Track 12A (now the propagation half drops too). The Track 12A T2-A decision intentionally accepts the symmetry break ("abort = the user does not trust this pull").
- **Pros of revisiting:** True abort-transactionality (all-or-nothing per pull): collect outcomes in `_download_and_apply` without applying, then either commit-all on completion or discard-all on abort.
- **Cons:** Significant restructure of `_pull_one_source` and `_download_and_apply` to defer writes; loses per-file isolation on errors; not aligned with how the rest of `mm pull` handles partial failures (corrupt peers, blob misses, etc.).
- **Context:** This is the broader pre-existing abort semantics, not a Track 12A regression. Revisit if user abort flow becomes a common pattern. Start at `_pull_one_source`'s outcome accumulation + `_download_and_apply`'s per-file write side-effects.
- **Depends on / blocked by:** None.
