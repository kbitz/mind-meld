# Grok usage-source contract (census 2026-09-14)

**Host version: Grok 1.0.30** — the installed host at the census, not a
compatibility proof across every intervening release and not the provenance
of every historical record. Bound to `host_usage.GROK_USAGE_CENSUS_HOST_VERSION`
by `test_contract_census_pin_matches_src_constant`. Re-census on any Grok minor bump.

Census history: 2026-08-17 on 1.0.4; 2026-08-27 on 1.0.5;
2026-09-04 on 1.0.13; 2026-09-10 on 1.0.25; 2026-09-14 on 1.0.30.

Do not confuse this pin with the four `Grok 1.0.5` mentions in README.md —
those are a different census (skill discovery via `grok inspect --json`,
verified 2026-08-24).

Grok 1.0.30 persists sessions at `~/.grok/sessions/<encoded-cwd>/<session-id>/`.
`updates.jsonl` is the authoritative update stream. A completed turn is a
terminal metadata record:

```text
timestamp (timezone-aware, unix seconds or ISO-8601)
params.update.sessionUpdate == "turn_completed"
params.update.prompt_id
params.update.stop_reason
params.update.usage.{input,output,reasoning,cachedRead,cacheCreation}Tokens
params.update.usage.modelUsage.<model-id>  (same counters)
params.update.elapsed_ms   (ignorable; present after the 2026-09-01 cutover)
```

The terminal record contains none of `content`, `rawInput`, or `rawOutput`.
Those appear on other update shapes and are ignored. `chat_history.jsonl`,
`signals.json`, `summary.json`, and logs remain forbidden.

Each accepted record is a per-prompt total, not a session-cumulative
restatement. `reasoningTokens` is a bounded subset of `outputTokens` and is
never added a second time. `elapsed_ms` is not read, not stored, and not
compared on resume.

## Observed `params.update` key sets on `turn_completed`

Census 2026-09-14 on installed Grok 1.0.30: 188 ledgers / 390.6 MB /
312 terminal records, 308 with usage, 4 usage-less, 4 `usageIsIncomplete`.
Corpus span: 2026-08-17 → 2026-09-14. Separately, 33 terminals written after
2026-09-10T18Z (the 1.0.25 census) span 3 UTC days; all have the modeled
`elapsed_ms` shape. These are observations of retained records, not proof
that every historical record was produced by 1.0.30.

The same four terminal key sets and two usage key sets remain. One model,
`grok-4.6-build`; zero multi-model turns; zero nonzero cache writes. The real
reader returned `complete=True`, 12 days, 3 partial days, cold scan 1.94 s.
`background_tasks` is newly observed (63 records), non-terminal, and ignored
before terminal classification. Outer keys are `{method, params, timestamp}`;
params keys are `{_meta, sessionId, update}` in all 312 terminals.

| key set | records | disposition |
|---|---|---|
| `{prompt_id, sessionUpdate, stop_reason, usage}` | 126 | modeled — counted |
| `{elapsed_ms, prompt_id, sessionUpdate, stop_reason, usage}` | 182 | modeled — counted (ignorable key dropped before the projection) |
| `{prompt_id, sessionUpdate, stop_reason}` | 3 | usage-less skip (`return []`), tallied as `usage_less_skipped` |
| `{elapsed_ms, prompt_id, sessionUpdate, stop_reason}` | 1 | usage-less skip. Load-bearing: miss this and 1 of 312 silently changes category |

An *unknown* extra non-content key on a terminal is still `unsupported`.
Exact-match on the required key set (after subtracting `_GROK_IGNORABLE_KEYS`)
is deliberate: T3 isolates the punishment (Grok drops, declared; Codex
unaffected), so the detector stays. Track 46A allowlists `elapsed_ms` only;
Track 46B owns per-record quarantine of unknown keys.

In the historical 2026-09-04 census, `stop_reason` observed: `end_turn` (210), `cancelled` (19). `cancelled` is
not a proxy for "spent nothing" — cancelled turns with a full `usage` block
are counted.

## Historical `elapsed_ms` date cutover (2026-09-04 census)

Observed date boundary in that corpus. Zero mixing on either side of the boundary.
Last record without `elapsed_ms`: 2026-08-19 19:06 UTC. First record with
it: 2026-09-01 12:48 UTC. 49 of 111 ledgers contain at least one drifted
record.

| record date | with `elapsed_ms` | without |
|---|---|---|
| 2026-08-14 | 0 | 18 |
| 2026-08-17 | 0 | 90 |
| 2026-08-18 | 0 | 38 |
| 2026-08-19 | 0 | 1 |
| 2026-09-01 | 16 | 0 |
| 2026-09-02 | 57 | 0 |
| 2026-09-03 | 9 | 0 |

Field census: `int` in 82 of 82 occurrences. Range 7,441 – 3,101,044
(7.4 s – 51.7 min). No correlation with `stop_reason` (present on 75
`end_turn` + 7 `cancelled`; absent on 135 `end_turn` + 12 `cancelled`).
It does not modify terminal state, carries no token counter, and is not
read by any mapping. 0 duplicate terminal keys corpus-wide. 0 drifted
records with an unparseable timestamp.

The `usage` sub-shape did **not** drift across this cutover.

## `usage` key sets (presence-only; not exact-match)

The reader validates required counter *presence* inside `usage`, never
`usage`'s key set.

| shape | disposition |
|---|---|
| required counters present | accepted |
| required counters plus `usageIsIncomplete: true`, minus `costUsdTicks` | **accepted, day marked partial** |

Live census 2026-08-30, Grok 1.0.5, 91 `updates.jsonl` / 219 terminal
records: exactly two distinct `usage` key sets. `usageIsIncomplete: true`
⟺ `costUsdTicks` absent, 100% correlation, no third shape. 3 flagged
turns = 1.4% of records but 8.43% of four-counter volume; the largest
turn in the corpus is one of them. The count did not grow while the
corpus went 193 → 219 in three days.

**Fidelity caveat — discharged, Track 34A / v0.12.50.** Turns Grok flags
`usageIsIncomplete: true` still contribute their counters (they are
usable totals) and the UTC day is carried in `HostUsageResult.partial_days`,
persisted on the Grok cache entry, intersected with the snapshot `keep`
set, and emitted as additive `partial_sources`. Pre-34A cache entries
are detected by key-absence of `partial_days` and re-walked once — not a
`CACHE_VERSION` bump. The caveat is kept so the discharge is visible;
do not delete it.

## Retained history and census recipe

The 18 terminal records dated 2026-08-14 present on 2026-09-04 no longer
exist in the 2026-09-14 corpus; the earliest retained day is now 2026-08-17.
Grok deleted old sessions. This is an observation, not a retention policy.
Retained totals can fall; observed endpoints do not prove continuous coverage.

For the next census, record the installed host version and observation time
separately from the corpus span. Walk the authoritative update ledgers once,
parse JSON lines, and count update kinds. For `turn_completed`, count distinct
outer, params, update, usage and modelUsage-bucket key sets; usage-less and
`usageIsIncomplete` records; multi-model turns; nonzero cache writes; and UTC
day extents. Count records after the prior census separately. Report key names
and counts only: never content, filesystem paths, session ids or prompt ids.
Run the real reader with its cache redirected to a disposable test directory;
record completeness, partial-day count and elapsed time without mutating the
live cache. Compare shapes to this contract before moving the pin.

## Fatal checks that stay fatal

Live corpus at census: one of thirteen fatal checks fired (the usage-less
key set, now a skip). The other twelve have five-to-eight orders of
magnitude of headroom (prompt_id and model id well under 256-byte caps;
counters well under 2^53; no `reasoning > output`; no divergent duplicates).
They stay fatal because they are not "too strict" — they have never fired
on a well-formed ledger, and an actual violation is a real wire break.

A session directory with `summary.json` and no `updates.jsonl` is a skip,
not `io_error`.

## Fixtures (sanitized; no real session content)

| path | what it pins |
|---|---|
| `workspace/session-a/updates.jsonl` | modeled terminal |
| `two-model/workspace/session/updates.jsonl` | two distinct Grok models in one session |
| `usage-less/workspace/session/updates.jsonl` | usage-less cancelled terminal |
| `cancelled-with-usage/workspace/session/updates.jsonl` | cancelled terminal *with* usage |
| `incomplete-usage/workspace/session/updates.jsonl` | `usageIsIncomplete: true` |
| `no-ledger/workspace/session/summary.json` | session dir lacking `updates.jsonl` |
| `census-1.0.30/workspace/session/updates.jsonl` | sanitized recent 1.0.30-census terminal with the live params `_meta` shape |
| `elapsed-ms/workspace/session/updates.jsonl` | sanitized real 1.0.13 modeled terminal with `elapsed_ms` |
| `usage-less-elapsed-ms/workspace/session/updates.jsonl` | sanitized real 1-in-229 usage-less terminal with `elapsed_ms` |

Fixture provenance: these fixtures contain no real session content,
identifiers, prompts, responses, tool data, credentials, or fleet token
magnitudes. The two 1.0.13 fixtures keep the live outer shape
(`method: "_x.ai/session/update"`, `params.sessionId`, `_meta`) and the
live `usage` key set (`costUsdTicks`, `apiDurationMs`, `modelCalls`);
identifiers and magnitudes are replaced.

The 1.0.30 fixture was derived from a post-2026-09-10 terminal during this
implementation; counters, identifiers and timestamps are replaced. Earlier
fixtures retain their original 1.0.13 provenance. The pin records the census
host version, not fixture provenance.
