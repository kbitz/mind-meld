# Grok usage-source contract (census 2026-08-17)

Grok 1.0.4 persists sessions at `~/.grok/sessions/<encoded-cwd>/<session-id>/`.
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

Fixture provenance: this fixture contains no real session content,
identifiers, prompts, responses, tool data, or credentials.
