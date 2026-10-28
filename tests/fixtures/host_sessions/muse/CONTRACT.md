# Muse usage-source contract (updated 2026-10-27)

**Host version: Muse 1.4.14** — the host version for the current record shapes, not a
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
payload.event.model      (optional for validated zero-token completions)
payload.event.usage.{input,output,cached,reasoning}_tokens
payload.event.usage.{cache_write,cache_read}_tokens  (optional)
payload.event.duration_ms, payload.event.finish_reason  (finish_reason optional)
```

## Muse 1.4.14 record shapes (2026-10-27)

The reader ignores additive event metadata, including
`estimated_prompt_tokens`. The four usage keys `input_tokens`, `output_tokens`,
`cached_tokens` and `reasoning_tokens` are required; only `cache_read_tokens`
and `cache_write_tokens` may be added. An omitted cache-read counter defaults
to `cached_tokens`; an omitted cache-write counter defaults to zero. An
explicit cache-read counter must still equal `cached_tokens`.

Zero-token completions without a non-empty string model are skipped, after
validating the envelope id, timestamp, usage keys, counter values and relations,
and the session-directory step-lead rule. A nonzero completion requires a
non-empty bounded string model. These shapes are covered by synthetic tests;
the historical corpus counts below describe Muse v1.4.7 only.

## Census (2026-10-14, one Mac, Muse v1.4.7)

179 `session.jsonl` files (37 root sessions, 142 subagent logs),
62,593 event envelopes, 1,462 `model_completed` events, corpus span
2026-10-13 → 2026-10-14. Key names and counts only; no content was read.

- Event key sets on `model_completed`: `{duration_ms, finish_reason,
  kind, model, usage}` (1,356) and `{duration_ms, kind, model, usage}`
  (106). These are the historical key sets; the current reader also accepts
  additive metadata and the model-less zero-token shape described above.
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
  files (retained frames are appended mid-file and restart the
  sequence), so the reader never keys on it; the dedup key is the
  envelope `id` alone, corpus-wide.

Re-census at review (2026-10-14, same Mac, Muse v1.4.7, 212 files,
87,539 envelopes, 2,020 `model_completed`): zero duplicate envelope
ids, none shared across files; 0 of 462 subagent steps reappear in
the parent log as an identical (model, six counters) tuple; every
retained frame sits mid-file (none on line 0); and the two carriers'
per-file input+output sums are equal in all 206 files with usage.
Logs present at the first census were appended, never rewritten:
re-reading against that cache resumed 7, added 33, re-walked 0 and
lost 0 counted steps. Sessions run past midnight: 37 of 215 logs were
last modified the day after their `YYYY/MM/DD` directory date, and
246 of 2,108 steps were recorded on the next UTC day, and none on an
earlier day than their directory. A two-day corpus cannot show
in-place resumes days later, so the reader's freeze rule
(`_MUSE_SETTLE_DAYS`, `_MUSE_SETTLE_IDLE_NS`) is backed by its
frozen-day stat check rather than by this span; see the events/retro
invariants.

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

Unknown usage key, `cached != cache_read`, `reasoning >
output`, a missing/oversize envelope id, an oversize model or missing model on
a nonzero completion, and a conflicting
duplicate envelope id (in one log or across logs, including the same
id under another model) refuse the whole reader as `unsupported`
(Track 31A isolates the drop to this reader). A validated zero-token
model-less step is skipped before dedup, so it never conflicts with a
counted turn carrying the same envelope id. A `recorded_at` before
2020-01-01 UTC, more than a day in the future, or longer than 20
digits is `malformed`. A step recorded more than one UTC day before its
session directory's date (history copied into a later session) is
`unsupported`. None of these fired on the census corpus.

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
ids (within and across logs), per-carrier per-file sums (to re-prove
the attribution restatement), and how many days after its directory
date each log is still appended to (to re-prove the freeze rule). Report key names and counts only. Run the real reader
with its cache redirected to a disposable directory.
