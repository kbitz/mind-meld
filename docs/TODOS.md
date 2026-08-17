# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

Single source of truth — there is no root-level `TODOS.md`. The two files were
reconciled on 2026-08-14; the root file's live inbox won and moved here, and the
`## Inbox` heading was renamed to `## Unprocessed` (what `/roadmap` drains).

## Unprocessed

- **GC plan-and-apply unification** — consider a machine-readable outcome that
  includes orphan blobs only when automation/audit export creates a real
  consumer; Track 17D intentionally limits its outcome seam to retention.
- **Tmp reaper age policy** — retain immediate, device-scoped crash cleanup
  unless evidence shows active writes can be swept; do not add an age gate as
  incidental Track 17D scope.
- **Host cache GC reaper** — `mm gc` reaps `session-tokens.json`, but not the
  three host caches (`host-tokens.json`, `grok-host-tokens.json`, and
  `opencode-host-tokens.json`). Mirror `retention._gc_token_cache`: drop
  entries whose dev/ino no longer resolves or whose day is outside retention,
  register it in `mm gc` and `--dry-run`, and add the retention routing row.
  Partial Codex scans only prune after a complete pass, so without this a
  repeatedly incomplete scan retains deleted-rollout entries forever.
- **In-flight rollout false degradation** — `_read_rollout` reports `stale`
  when a file changes mid-read and `partial` for a trailing unterminated line.
  Track 19A turns either single-file refusal into an omitted whole snapshot and
  an `autopush` `degraded` breadcrumb; because active rollouts sort last, one
  actively written file can discard already staged totals. Consider skipping a
  stale or partial final rollout instead: the next push restates cumulative
  totals.
- **Warm host scan remains O(all files)** — cache hits still calculate
  `_cache_key` and `_fingerprint` for every rollout. On a 455-rollout corpus,
  the warm scan costs 25ms (14.6ms fingerprinting and 4.0ms cache keys), and
  will cross the 250ms autopush budget at roughly 4,500 rollouts. Consider
  bypassing fingerprints for files older than the last complete scan, keying on
  `(dev, ino)`, or bounding `_iter_rollouts` by date directory.
- **Host day buckets are mutable lifetime snapshots** — a
  `host-usage-snapshot` bucket is the lifetime total of every session last
  touched on that UTC day, not that day's spend. Resuming an old session moves
  its whole total, so historical buckets can decrease. Only
  latest-row-per-device is safe; Track 20A must lock the wire contract before
  Track 21A renders `active_days` as a time series.

_Last updated 2026-08-16 by Track 19A follow-up._
