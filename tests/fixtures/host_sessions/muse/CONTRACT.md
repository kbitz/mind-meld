# Muse usage-source contract (census 2026-10-14)

**Host version: Muse 1.4.7** — the installed host at the census, not a
compatibility proof across releases. Bound to
`host_usage.MUSE_USAGE_CENSUS_HOST_VERSION` by
`test_muse_contract_census_pin_matches_src_constant`. Re-census on any
Muse minor bump.

Muse persists session logs at
`~/.local/share/muse/sessions/YYYY/MM/DD/<session-id>/session.jsonl`,
with subagent sessions nested at
`<session-id>/subagent/<child-id>/session.jsonl`. Each file is an
append-only event stream. A completed model step is one envelope:

```text
record_type == "event", payload_type == "runtime.session"
id                      (envelope id; globally unique in the corpus)
recorded_at             (microsecond epoch, decimal text)
payload.event.kind == "model_completed"
payload.event.model
payload.event.usage.{input,output,cached,cache_write,cache_read,reasoning}_tokens
payload.event.duration_ms, payload.event.finish_reason  (finish_reason optional)
```

## Census (2026-10-14, one Mac, Muse 1.4.7)

179 `session.jsonl` files (37 root sessions, 142 subagent logs),
62,593 event envelopes, 1,462 `model_completed` events, corpus span
2026-10-13 → 2026-10-14. Key names and counts only; no content was read.

- Event key sets on `model_completed`: `{duration_ms, finish_reason,
  kind, model, usage}` (1,356) and `{duration_ms, kind, model, usage}`
  (106). The reader accepts any superset of `{kind, model, usage}`
  inside that five-key universe; anything else is `unsupported`.
- `usage` key set is exactly the six counters in all 1,462 records.
- Counter relations held in every record: `cached_tokens ==
  cache_read_tokens`; `cache_read + cache_write <= input`;
  `reasoning <= output`; all counters non-negative ints. Cache writes
  were zero in every record. Counters are therefore inclusive
  (`input` contains the cache reads) and are disjoint-normalized with
  the shared three-term rule, like Codex and Grok.
- Models observed on `model_completed`: `muse-wonder-full-1.0`
  (1,397), `muse-spark-1.3` (46), `muse-wonder-1.0` (19).
- Envelope `id` values: 62,593 unique, zero duplicates corpus-wide.
- `sequence` is strictly increasing in only 164 of 174 then-present
  files (retained frames rewrite history), so the reader never keys
  on it; the dedup key is the envelope `id` scoped by session dir.

## The second carrier (never read)

`goal_usage_attribution` events (`payload.event.record.quantity`,
split by `usage_family`) restate the same model steps. Per-file
input+output sums of the two carriers were equal in 47 of 55 files
with usage; the rest were in-flight sessions whose attribution had
not caught up. Counting both carriers would double every total, so
only the terminal `model_completed` event is read and attribution
records are skipped uninterpreted, like every other non-carrier
record (other event kinds, `status` records, retained frames).

## Fatal checks that stay fatal

Unknown event/usage key, `cached != cache_read`, `reasoning >
output`, a missing/oversize envelope id or model, and a conflicting
duplicate envelope id refuse the whole reader as `unsupported`
(Track 31A isolates the drop to this reader). A `recorded_at` before
2020-01-01 UTC or more than a day in the future is `malformed`. None
of these fired on the census corpus.

## Fixtures (sanitized; no real session content)

| path | what it pins |
|---|---|
| `2026/10/13/session-a/session.jsonl` | two modeled steps, two models; one attribution restatement (not counted); one non-usage event; one retained frame |
| `2026/10/13/session-a/subagent/child-a/session.jsonl` | subagent log is walked and counted |
| `2026/10/14/session-b/session.jsonl` | `finish_reason`-absent shape; next-day bucketing |

Fixture provenance: shapes mirror the census key sets above;
identifiers, magnitudes and timestamps are invented. No real session
content, prompts, responses, tool data or credentials appear.

For the next census: record the installed Muse version separately
from the corpus span; count envelope kinds, `model_completed` event
and usage key sets, counter-relation violations, duplicate envelope
ids, and per-carrier per-file sums (to re-prove the attribution
restatement). Report key names and counts only. Run the real reader
with its cache redirected to a disposable directory.
