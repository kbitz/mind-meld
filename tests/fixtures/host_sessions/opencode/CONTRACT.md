# OpenCode usage-source contract (census 2026-08-16)

This is a version-neutral compatibility contract: the local machine did not
have an OpenCode data directory from which to establish a reliable host
version. The modern allowlisted source is `~/.local/share/opencode/opencode.db`, table
`message`, column `data`. The reader opens it with SQLite `mode=ro`,
`query_only`, a zero-wait busy policy, and one read transaction. It projects
only `id`, `role`, `modelID`, `time.completed`, `error`, and token counters;
it never selects message text, parts, provider configuration, environment,
credentials, or session metadata.

The legacy source is
`~/.local/share/opencode/storage/message/<session-id>/<message-id>.json`. It
contains complete message records, including transcript and environment data,
so it is expressly unsupported. The adapter sees the legacy root only to
return `unsupported`; it never opens or deserializes a legacy message file.

Only a SQLite assistant message with a completion time, no `error`, a known
successful `finish` (`stop`, `tool-calls`, `length`, or `content-filter`),
valid bounded non-zero counters, and a unique ID is terminal. Completed
all-zero helper records without a `finish` are explicitly non-billable and
skipped; a zero ledger that claims a terminal finish is rejected. Numeric
timestamps are Unix milliseconds (or seconds for the pre-epoch-compatible
fixture variant) and are attributed to the UTC date. OpenCode stores
non-reasoning output and reasoning separately, so the adapter adds them into
Mind Meld's single output bucket. Unknown schemas, malformed JSON, duplicate
IDs, changing files, a SQLite lock, a legacy-only source, or an in-progress
migration are incomplete; no source is a completed empty scan.

Literal aliases are empty for this census. `modelID` is passed directly to the
shared strict `host_family()` classifier. Re-census before accepting renamed
keys, a different SQLite table, a new legacy path, or new migration behavior.
If both modern and legacy sources are present, do not choose: return the
explicit migration diagnostic.

Fixture provenance: the SQLite schema is a minimal synthetic contract table.
No real OpenCode database, prompt, assistant response, provider settings, or
credentials was copied into the repository.
