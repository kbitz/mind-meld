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
| required counters plus `usageIsIncomplete: true`, minus `costUsdTicks` | **accepted-and-ignored** |

**Fidelity caveat.** Turns Grok flags `usageIsIncomplete: true` are counted
as complete accounting. mm has no partial-fidelity channel on
`HostUsageResult`. Stated here rather than deferred: the loose `usage` key
set absorbed this field with silent fidelity loss, not "zero harm". Track
34A owns a coverage state if one is added.

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
