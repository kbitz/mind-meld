# Cursor via Conductor usage contract (Track 67A)

Censused 2026-09-23 on one Mac. Cursor CLI: **2026.09.18-9a7762b**.
Persisted schema producer: **Conductor 0.87.3**, read from the installed
app's CFBundleShortVersionString and CFBundleVersion during implementation.
The run rows have no schema version. Both versions are pinned in host_usage.py;
pins document provenance, not compatibility with another release.

These are the fleet's first two Cursor sessions: three finished runs, two
sessions, one model (`grok-4.7`), one parameter combination (`fast=false`,
`reasoning_effort=high`). Total: **32,729,640 tokens**. Rows are real samples,
not hand-built substitutes. Redaction projects only runId, turnNumber, status,
model, usage, createdAt, startedAt and endedAt. Session directory names are
replaced; identifiers and counters are unchanged. No agents.ndjson (cwd and
blobEncryptionKey), result, error, events, checkpoints or transcripts ship.

Only `status == "finished"` with non-null usage contributes counters. The file
is rewritten in place, one line per runId: running/null becomes finished/usage.
Every stable read replaces that run's previous day/model/counter contribution.
The four counters are **disjoint**: inputTokens + outputTokens + cacheReadTokens
+ cacheWriteTokens equals totalTokens in all three samples. reasoningTokens
is a bounded subset of outputTokens, never an additional charge. The complete
turn belongs to endedAt's UTC date, even if it started before midnight.

Never observed: fast=true; composer-2.5/auto; nonzero cacheWriteTokens;
non-null usageRef; error/cancelled rows; a midnight-spanning turn; a second
producing Mac. Synthetic mutations in tests exercise these failure paths and
are explicitly not additional census evidence. Unknown statuses, missing
usage fields, invalid arithmetic and unknown shapes fail visibly. Nonzero
cache writes label the day partial; counters still publish with disjoint-v1
after the identity check, and an unpublished cache-write price remains unknown.
usageRef with null usage retains a partial placeholder. If its day has no
known token bucket, the reader refuses as partial, since the existing wire
would otherwise discard that warning. If the day has known usage, it publishes
with partial_sources. No synthetic zero tokens are invented.

The private cursor-host-tokens.json stores hashed run IDs, model IDs, UTC days
and counters. It uses the shared CACHE_VERSION and reader-owned
CURSOR_HOST_CACHE_RETENTION_DAYS = 90 (not events.CURSOR_SCAN_DAYS). Whole stable
files learned before a deadline commit, never a partially parsed file.
Repeated short passes are not guaranteed to converge: each rewritten file
must be reparsed. A larger read budget or attended warming can finish the scan.
The cache keeps runs removed by Conductor, whose retention is unknown. It
cannot recover runs pruned before mm first saw them. Temp-file fsync, atomic
rename and directory fsync protect the authoritative history; corrupt reads
refuse without replacing it. This bounds future loss, not historical backlog.

Coverage means **Cursor via Conductor**, never all Cursor use. Bare cursor-agent
persists no billing ledger; context-window token counts are not usage. A mixed
Conductor/bare-CLI Mac has invisible bare-CLI usage even on a successful scan.
No Conductor store and no prior cache returns no_metadata_ledger; format drift
returns malformed/unsupported. No SQLite database or content-bearing sibling
is ever opened.

Prices are API **list-rate equivalents**, not spend on the censused Cursor
Ultra subscription. Cursor's rates were fetched 2026-09-23:
https://cursor.com/docs/models-and-pricing and
https://cursor.com/docs/models/grok-4-7. Standard Grok 4.7 is $2 input,
$0.50 cache read and $6 output per million tokens. Above 256k input per request
those rates double; per-turn totals cannot recover that tier, so cost is a
floor. Cache-write price is unpublished, not demonstrated to be zero.

Fast mode bills at 2x standard, or 3x standard for long context, and is the
default on Pro and higher. Its tokens use the deliberately unpriced
grok-4.7-fast ID. Fast-only rows show cost `—`; mixed rows show `≥` for the
priced subtotal, omitting Fast cost entirely. Effort has no separate published
rate. auto/composer remain Unclassified; adding a family is deferred until
real use is observed. Grok Build history remains complementary, not redundant.
