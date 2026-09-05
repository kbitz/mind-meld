# Grok usage-source contract (census 2026-09-04)

**Host version: Grok 1.0.13** (previous census 2026-08-27 was Grok 1.0.5;
2026-08-17 was Grok 1.0.4). Bound to `host_usage.GROK_USAGE_CENSUS_HOST_VERSION`
by `test_contract_census_pin_matches_src_constant`. Re-census on any Grok
minor bump; grep this file for `Grok 1.0.13`.

Do not confuse this pin with the four `Grok 1.0.5` mentions in README.md —
those are a different census (skill discovery via `grok inspect --json`,
verified 2026-08-24).

Grok 1.0.13 persists sessions at `~/.grok/sessions/<encoded-cwd>/<session-id>/`.
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

Live census 2026-09-04, device 889e42c0, Grok 1.0.13: 111 ledgers / 261.4 MB
/ 229 terminal records.

| key set | records | disposition |
|---|---|---|
| `{prompt_id, sessionUpdate, stop_reason, usage}` | 144 | modeled — counted |
| `{elapsed_ms, prompt_id, sessionUpdate, stop_reason, usage}` | 81 | modeled — counted (ignorable key dropped before the projection) |
| `{prompt_id, sessionUpdate, stop_reason}` | 3 | usage-less skip (`return []`), tallied as `usage_less_skipped` |
| `{elapsed_ms, prompt_id, sessionUpdate, stop_reason}` | 1 | usage-less skip. Load-bearing: miss this and 1 of 229 silently changes category |

An *unknown* extra non-content key on a terminal is still `unsupported`.
Exact-match on the required key set (after subtracting `_GROK_IGNORABLE_KEYS`)
is deliberate: T3 isolates the punishment (Grok drops, declared; Codex
unaffected), so the detector stays. Track 46A allowlists `elapsed_ms` only;
Track 46B owns per-record quarantine of unknown keys.

`stop_reason` observed: `end_turn` (210), `cancelled` (19). `cancelled` is
not a proxy for "spent nothing" — cancelled turns with a full `usage` block
are counted.

## `elapsed_ms` date cutover

Hard producer-version cutover. Zero mixing on either side of the boundary.
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
| `elapsed-ms/workspace/session/updates.jsonl` | sanitized real 1.0.13 modeled terminal with `elapsed_ms` |
| `usage-less-elapsed-ms/workspace/session/updates.jsonl` | sanitized real 1-in-229 usage-less terminal with `elapsed_ms` |

Fixture provenance: these fixtures contain no real session content,
identifiers, prompts, responses, tool data, credentials, or fleet token
magnitudes. The two 1.0.13 fixtures keep the live outer shape
(`method: "_x.ai/session/update"`, `params.sessionId`, `_meta`) and the
live `usage` key set (`costUsdTicks`, `apiDurationMs`, `modelCalls`);
identifiers and magnitudes are replaced.
