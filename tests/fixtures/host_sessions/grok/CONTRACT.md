# Grok usage-source contract (census 2026-08-27)

**Host version: Grok 1.0.5** (previous census 2026-08-17 was Grok 1.0.4).
Both defects Track 31A closed — an absent `updates.jsonl` treated as
`io_error`, and a usage-less `turn_completed` treated as `unsupported` —
were wire drift this census never covered. Re-census on any Grok minor
bump; grep this file for `Grok 1.0.5`.

Grok 1.0.5 persists sessions at `~/.grok/sessions/<encoded-cwd>/<session-id>/`.
`updates.jsonl` is the authoritative update stream. A completed turn is a
terminal metadata record:

```text
timestamp (timezone-aware, unix seconds or ISO-8601)
params.update.sessionUpdate == "turn_completed"
params.update.prompt_id
params.update.stop_reason
params.update.usage.{input,output,reasoning,cachedRead,cacheCreation}Tokens
params.update.usage.modelUsage.<model-id>  (same counters)
```

The terminal record contains none of `content`, `rawInput`, or `rawOutput`.
Those appear on other update shapes and are ignored. `chat_history.jsonl`,
`signals.json`, `summary.json`, and logs remain forbidden.

Each accepted record is a per-prompt total, not a session-cumulative
restatement. `reasoningTokens` is a bounded subset of `outputTokens` and is
never added a second time.

## Observed `params.update` key sets on `turn_completed`

| key set | disposition |
|---|---|
| `{prompt_id, sessionUpdate, stop_reason, usage}` | modeled — counted |
| `{prompt_id, sessionUpdate, stop_reason}` | usage-less skip (`return []`), tallied on the Grok cache as `usage_less_skipped` |

An extra non-content key on a terminal is still `unsupported`. Exact-match
on the `update` key set is deliberate: T3 isolates the punishment (Grok
drops, declared; Codex unaffected), so the detector stays.

`stop_reason` observed: `end_turn`, `cancelled`. `cancelled` is not a proxy
for "spent nothing" — cancelled turns with a full `usage` block are counted.

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

Fixture provenance: these fixtures contain no real session content,
identifiers, prompts, responses, tool data, credentials, or fleet token
magnitudes.
