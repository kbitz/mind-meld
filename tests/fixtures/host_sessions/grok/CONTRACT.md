# Grok Build usage-source contract (Track 110A, census 2026-11-05)

**Grok Build 1.0.70** (`grok 1.0.70 (f50904eaee6c) [stable]`) is the host version for the
record shapes below, not a compatibility proof across releases. Bound to
`hosts.grok.GROK_USAGE_CENSUS_HOST_VERSION` by `tests/test_grok_usage.py`. Re-census on any
Grok Build minor bump. This replaces the retired `updates.jsonl` reader's contract
(`docs/archive/grok-build-usage-reader.md`); nothing here reads conversation bytes.

## Source choice

The one usage source is **`~/.grok/sessions/<cwd-dir>/<session-id>/usage.json`**, never
`updates.jsonl` (its `turn_completed` records carry the same counters, but that file is the
"ACP session update stream (conversation + tool calls)"; 74 of 76 planning-census sessions would be
opened to find nothing). Why `usage.json`:

- it holds metadata only, so the reader never decodes prompts, responses or tool output;
- an empty session has no `usage.json` at all: one `lstat` says "nothing here" and nothing is opened;
- Grok documents the same envelope through `grok usage <id>` (user guide `17-sessions.md`).

Every conditional check passed: every session with a `turn_completed` has a `usage.json`
(0 without one); rows are per turn; each row has a timezone-aware `endedAt` and per-model
`modelUsage`; a fork repeats its parent's rows byte-identically; subagent usage is not recorded
twice in `usage.json` (below). The file is not a documented stable contract, so a format change
fails this reader closed (see Validation).

## Census (2026-11-05 UTC, machine 349C-kb-ms, Grok Build 1.0.70)

Key names, counts and booleans only; no prompt, path or counter value was recorded.

| Fact | Value |
|---|---|
| cwd directories / sessions | 67 / 83 (every session directory is named by a UUID) |
| sessions with `usage.json` | 5 (3 of them made by this census: a two-turn resume, its fork, a subagent run) |
| empty sessions | 78: no `updates.jsonl` 18, 1-line 21, 2-line 39 (hook_execution updates only) |
| `turn_completed` without `usage.json` | 0 |
| files in every session | `chat_history.jsonl`, `events.jsonl`, `summary.json`, `system_prompt.txt` |
| model ids | `grok-4.7-build` only |
| `usage.json` top keys (5 of 5) | `sessionId`, `updatedAt`, `session`, `turns` |
| `session` keys (5 of 5) | the eight counters, `modelCalls`, `costUsdTicks`, `turnCount`, `primaryModelId`, `modelUsage` |
| turn row keys (8 of 8) | the `session` counters, `turnNumber`, `endedAt`, `turnCount`, `primaryModelId`, `modelUsage` |
| `modelUsage` entry keys (all) | `inputTokens`, `outputTokens`, `cachedReadTokens`, `cacheCreationTokens`, `reasoningTokens`, `totalTokens`, `modelCalls`, `costUsdTicks` |

The eight counters are `inputTokens`, `outputTokens`, `cachedReadTokens`, `cacheCreationTokens`,
`reasoningTokens`, `totalTokens`, `modelCalls` and `costUsdTicks`.

Relations held in every one of 8 rows: `totalTokens == inputTokens + outputTokens`;
`cachedReadTokens <= inputTokens`; `reasoningTokens <= outputTokens`; `cacheCreationTokens == 0`;
per-model sums equal the row; `endedAt` carries a UTC offset; `sessionId` equals the directory name.

## Semantics

- **Rows are per turn.** In the two multi-turn sessions the `session` object equals the sum of
  its `turns`; distinct rows have distinct counters; `turnCount` on a row is 1 and on `session`
  is the row count. The reader sums rows and never reads `session`.
- **`inputTokens` is inclusive of `cachedReadTokens`** (and of `cacheCreationTokens`, always 0 so
  far). The reader normalizes it with the shared inclusive rule
  (`host_usage._normalize_inclusive_counters`): uncached input = input - cacheRead - cacheCreate.
  Grok's headless JSON reports `input_tokens` uncached beside `cache_read_input_tokens`; that
  output is not read.
- **`reasoningTokens` is inside `outputTokens`** (`reasoning <= output` in every row) and is never
  added again.
- The turn's day is the UTC date of `endedAt`. `turnNumber` contributes only to the hashed
  dedup key; `totalTokens` validates the input/output identity. Neither is stored directly.
  `modelCalls`, `turnCount`, `costUsdTicks`, `session` and `primaryModelId` are ignored entirely.
- **Resume** appends rows to the same directory (a resumed session went from 1 to 3 rows) and
  replaces `usage.json` atomically (the inode changed across a turn).
- **Fork** creates a new session directory in the same cwd directory whose `usage.json` repeats
  the parent's rows byte-identically and adds its own; its `session` includes inherited history.
  Each turn is therefore keyed by `(endedAt, turnNumber, model)` and counted once across files.
- **Subagent** (one `spawn_subagent`-only headless run): the parent row's `modelCalls` rose to 10
  and no child directory or second `usage.json` appeared in the tree, so child usage is folded
  into the parent turn. The user guide says child sessions live in the normal tree; none was
  observed. Rule: a `usage.json` is counted whole; if a child ever gets its own file with
  distinct keys it would double count, and this contract needs a re-census (no way to tell
  it apart from content or directory names was found).

## Empty sessions

A session with no `usage.json` (no `updates.jsonl`, a 1-line one, or a 2-line one of
`hook_execution` updates, as Paseo leaves) is a cached zero contribution. The reader `lstat`s
`usage.json` only; it never opens `updates.jsonl` or any other file.

## Validation

Required (exactly what the reader uses): top-level `turns` (a list); per row `turnNumber`,
`endedAt`, `modelUsage` and the six token counters (`inputTokens`, `outputTokens`,
`cachedReadTokens`, `cacheCreationTokens`, `reasoningTokens`, `totalTokens`); the same six on
every `modelUsage` entry; `totalTokens == inputTokens + outputTokens`. Unknown extra keys at any
level (top, `session`, row, `modelUsage`) are ignored: Grok Build adds fields often, and refusing
them blacked out the retired reader for months. `session`, `sessionId`, `updatedAt`,
`modelCalls`, `costUsdTicks`, `turnCount` and `primaryModelId` are never read.

- missing required key, non-object, or broken `totalTokens` identity: `unsupported`
  (standing blocker; remedy `mm update`).
- bad JSON, non-integer, negative or over-2^53 counters, naive or unparseable `endedAt`, empty or
  oversize model id, `reasoningTokens > outputTokens`, per-model sums not equal to the row,
  `cachedRead + cacheCreation > input`, or one turn key with different counters in two files:
  `malformed`.
- a file replaced while being read: `stale` (retried on the next push).

## Fixtures

`sessions/cwd-a/` holds a single-turn session (`…a001`), a two-turn session (`…a002`), its fork
(`…a003`, repeats both rows, adds a third), and a two-model turn (`…a004`). `sessions/cwd-b/`
holds three empty sessions (`…b001` no usage files, `…b002` 1-line `updates.jsonl`, `…b003` 2-line).
All ids, directory names, counters and timestamps are synthetic.
