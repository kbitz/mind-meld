# Grok usage-source contract (census 2026-08-16)

Grok's documented persisted source is
`~/.grok/sessions/<encoded-workspace>/<session-id>/updates.jsonl`. It is the
authoritative conversation and tool-call stream. It is not an allowlisted
usage source: reading it would deserialize prompts, responses, tool inputs, or
tool outputs.

The documented headless `end` record has usage fields, but it is stdout
protocol output rather than a persisted metadata ledger. It also does not
carry enough stable model/timestamp information for the session reader to
attribute mixed-model usage safely. `signals.json` contains context-window
signals, not a billable per-turn accounting ledger; `logs/unified.jsonl` is an
internal log and remains forbidden. The adapter therefore returns the stable
`unsupported` diagnostic whenever the persisted session root exists, and a
completed empty result only when the root is absent.

Re-census only when Grok publishes a separately persisted, metadata-only,
versioned ledger with terminal status, UTC completion timestamp, model ID, and
the four required counters. Fixture provenance: this fixture contains no real
session content, identifiers, prompts, responses, tool data, or credentials.
