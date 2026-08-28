# Grok Build usage reader

## Outcome

`retro-fleet` must show Grok Build token usage from a Mac without a daemon,
server, browser automation, or cloud API. It will read only a strict terminal
usage record from Grok's local session-update stream, publish the existing
encrypted aggregate host snapshot, and render that aggregate with explicit
coverage.

This is an opt-in, metadata-only reader. Prompts, responses, tool calls,
paths, chat history, and raw session files never leave the Mac.

## Verified current state (2026-08-17)

Grok 1.0.4 persists sessions at `~/.grok/sessions/` (or beneath
`$GROK_HOME`). Its local session guide says sessions include token usage and
turn counts, and documents `updates.jsonl` as the authoritative update stream
alongside content-bearing files such as `chat_history.jsonl`
(`~/.grok/docs/user-guide/17-sessions.md:9-39`).

An inspected local v1 `updates.jsonl` contains a terminal usage projection:

```text
<outer timestamp>
  params.update.sessionUpdate == "turn_completed"
  params.update.prompt_id
  params.update.stop_reason
  params.update.usage
```

The observed `usage` projection contains `inputTokens`, `outputTokens`,
`reasoningTokens`, `cachedReadTokens`, `cacheCreationTokens`, `totalTokens`,
`numTurns`, and `modelUsage`. `modelUsage` carries the model identifier and
the same counter family. The terminal record contains none of `content`,
`rawInput`, or `rawOutput`; those appear on other update shapes and are out of
scope.

The original pre-18D reader returned `no_metadata_ledger`; the shipped reader
now accepts only the metadata-only terminal projection. It runs only when the
Grok source is enabled or the retained 21A bit is true. The fleet consumer
still controls whether accepted host-usage snapshots render on the card.

## Decisions

| Question | Decision |
|---|---|
| Collection model | Scan local persisted terminal records during `mm init` and substantive `mm push`; no resident process. |
| Consent | Add a local, explicit `[retro].grok_host_usage = true` setting, controlled by `mm enable-source grok` / `mm disable-source grok`. Absent means false. Flat bool, not a nested table: `patch_config_on_disk` cannot nest-patch. |
| Why not a zero-file `grok` sync source? | A zero-file source would mint an empty remote manifest and imply whole-home sync. Track 22B added a real Claude-shaped `type: "grok"` walker (`skills/` / `commands/` / `rules/` only) instead. |
| Data sent to the fleet | Existing encrypted `host-usage-snapshot` aggregate only: canonical model family, UTC day, four token counters, active-day coverage, and source name. |
| Unknown or changed Grok format | Fail closed per reader. Drop and declare Grok while retaining any completed Codex/OpenCode readers; omit the row only when no reader completed. |
| Cost | Do not estimate Grok subscription/API cost in this work. Usage volume and cost are different claims. |

Track 22B supersedes the source part of this original 21A design: `grok` is
now a real, Claude-shaped `[[sync.sources]]` row whose walker is hardcoded to
`skills/`, `commands/`, and `rules/`. Reusing `mm enable-source grok` still
keeps one verb, now both adding that scoped source and retaining the
`[retro].grok_host_usage` compatibility bit. The source is never a root walk:
credentials, session state, and whole-file configuration remain local.

## Reader contract

### Discovery and safe parsing

1. Resolve the root from `GROK_HOME`, falling back to `~/.grok`, at call time.
   Reject a symlinked, non-directory, unreadable, or changing root with the
   existing closed `Reason` vocabulary.
2. Traverse only regular, non-symlink `updates.jsonl` files directly under a
   session directory below `sessions/`. Do not read `summary.json`,
   `signals.json`, `chat_history.jsonl`, `events.jsonl`, terminal logs, plans,
   prompts, or tool outputs.
3. Read binary JSONL one bounded line at a time. Decode and discard each line
   immediately. Never log, cache, return, or synchronize a raw line, session
   path, prompt, response, tool call, or error text.
4. Accept a line only when its outer timestamp is timezone-aware and
   `params.update` is exactly the terminal metadata projection:
   `prompt_id`, `sessionUpdate`, `stop_reason`, and `usage`; its
   `sessionUpdate` must equal `turn_completed`. The exact same terminal shape
   without `usage` is a zero-token skip, tallied as `usage_less_skipped`
   before the exact-match check. A line whose update object carries a
   content-bearing field is not a terminal ledger record and is ignored, not
   inspected further.
5. Require a non-empty bounded `prompt_id`, a valid terminal stop reason, and
   a bounded model ID for every `modelUsage` entry. A malformed terminal
   record, duplicate terminal key with different counters, incomplete final
   JSONL line, or source mutation during the scan refuses the whole reader.
   A session with no terminal-ledger records is a completed zero contribution.

### Accounting and deduplication

- A terminal key is `(session directory UUID, prompt_id)`. Equal duplicate
  records count once; conflicting duplicates are unsupported and withhold the
  complete host snapshot. This prevents replay/resume duplication without
  treating a new prompt in an existing session as a replacement.
- Attribute each accepted terminal projection to the UTC calendar day of its
  outer timestamp. Unlike Codex's cumulative rollout total, a Grok completed
  turn is an individual event; an old session resumed today contributes its
  new turn today, not its historical total again.
- Aggregate `modelUsage` by model, then classify with the existing
  `host_usage.host_family`. Do not create a second model-family classifier.
- Map `inputTokens`, `cacheCreationTokens`, `cachedReadTokens`, and
  `outputTokens` to Mind Meld's `input`, `cache_create`, `cache_read`, and
  `output` buckets. `reasoningTokens` is validated as a bounded subset of
  `outputTokens` and is never added a second time. `totalTokens`,
  `numTurns`, durations, and cost ticks are validation-only metadata and are
  not sent or counted.

Before enabling the reader, add a sanitized fixture/probe that establishes two
semantics from the installed v1 format: (1) each accepted record is a
per-prompt terminal total rather than a session-cumulative restatement, and
(2) `reasoningTokens` is included in `outputTokens`. If either fails, retain
the current `no_metadata_ledger` behavior and do not ship a guessed mapping.

### Cache and budgets

`grok-host-tokens.json` becomes a versioned, private 0600 incremental cache.
It stores only opaque session-file keys, file fingerprints, byte offsets,
hashed terminal keys, bounded model IDs, UTC-day buckets, and aggregate token
counters. It never stores raw paths or any conversation bytes.

The cache follows the established Codex rules:

- a complete scan replaces the entry map and prunes deleted files;
- a deadline or later-file failure merges independently complete staged entries
  so repeated bounded pushes converge;
- a valid append resumes at the last complete-line offset only after head and
  old-tail fingerprints match; any doubt triggers a bounded full scan;
- a terminal-key digest set is required to suppress an appended replay without
  retaining its raw prompt ID;
- attended `mm init` and interactive `mm push` may warm Grok's cache once,
  then retry under the normal 500 ms capture budget; `autopush` never spends
  the warm budget.

The reader receives the existing fresh 250 ms `autopush` / 500 ms interactive
host-read deadline. A deadline, lock, stale file, malformed ledger, or I/O
error returns `complete=False`; `events_tail` drops and declares that reader,
while preserving every other reader that completed. It omits the whole row
only when none completed, and always leaves normal sync, git, and Claude
capture intact.

## Integration plan

```text
explicit local Grok consent
        |
        v
strict updates.jsonl reader + private incremental cache
        |
        v
reader-scoped host-usage snapshot (encrypted by mm-events sync)
        |
        v
latest accepted snapshot per device in retro-fleet
        |
        v
MODELS card: Claude sessions + coverage-aware Codex/Grok/other host totals
```

1. Add the local `[retro].grok_host_usage` bit behind `mm enable-source grok`
   / `mm disable-source grok`. Track 22B later added the same-name,
   hardcoded `type: "grok"` source and keeps this bit as a compatibility OR.
2. Change the reader gate so Grok is not invoked unless the source or local
   compatibility bit authorizes it. Codex and OpenCode remain source-gated.
3. Replace `_scan_grok_root` with the strict reader and migrate its marker-only
   cache to the versioned incremental form. Generalize the warm dispatch from
   one reader to an explicit set of warmable readers.
4. Update the host-snapshot aggregation path in
   `skills/retro_fleet/aggregator.py`: validate every accepted wire row using
   the invariant contract, select the newest complete row per device, retain
   its UTC buckets whole as last-known-good inventory, and preserve
   `token_sources` and `as_of` coverage. Never slice those lifetime
   last-touch totals to the requested retro window, sum them into fleet spend,
   or carry an older Grok view into a newer partial device view.
5. Keep host totals separate from `SessionsAggregate.tokens_by_model`, which
   is the Claude-only cost input. Render family rows from a combined display
   view, label the two sources, do not price host totals, and state missing or
   stale coverage as unknown rather than zero.
6. Update the host-usage invariant and README. Replace the old statement that
   Grok has no metadata ledger with the v1 support boundary and the opt-in
   command.

## Acceptance criteria

1. With Grok consent disabled, a push never opens `~/.grok` and emits no
   `grok` token source.
2. With consent enabled and a valid v1 fixture, a completed Grok turn appears
   once in the encrypted host snapshot under its canonical family and UTC day.
3. Replayed terminal updates, resuming a session, and repeated pushes do not
   double-count a prompt.
4. Malformed, content-bearing, incomplete, changing, locked, or unrecognized
   terminal data drops and declares only that reader. A host snapshot is
   omitted only when no reader completed, and the existing safe reader/reason
   breadcrumb still surfaces.
5. The reader/cache never contains a raw session path, prompt ID, prompt,
   response, tool result, chat-history byte, or terminal text. Tests assert
   this against serialized cache and event rows.
6. A cache-cold large fixture obeys the 250/500 ms deadline, preserves
   per-file progress, and converges after bounded pushes or one attended warm.
7. `retro-fleet` selects only complete latest snapshots, retains Grok totals
   as whole last-known-good inventory, and labels unavailable, stale, or
   disabled device coverage without converting it to zero or presenting it as
   requested-window spend.
8. Existing Codex, OpenCode, Claude-only token/cost, encryption, retention,
   and no-daemon behavior remain unchanged.

## Test matrix

| Layer | Coverage |
|---|---|
| `tests/test_config.py` | Grok source name/type boundaries, hardcoded-walker config shape, and retained usage-consent compatibility |
| `tests/test_host_usage.py` | strict v1 terminal acceptance, multi-model totals, UTC attribution, replay/conflicting duplicate handling, malformed/content/incomplete/stale/deadline/cache-resume cases |
| `tests/test_host_usage_snapshot.py` | Grok consent gate, warm dispatch, reader-scoped isolation/omission, and closed breadcrumb semantics |
| `tests/test_events.py` | serialized snapshot has only canonical aggregate fields and remains capped to 90 UTC days |
| `tests/test_retro_fleet_aggregator.py` | accepted-row validation/selection, whole-view/no-window-slice/no-fleet-spend isolation, host coverage gaps, and MODELS card labels |
| isolation fixtures | redirect Grok root and cache for every test; no test may touch a real `~/.grok` store |

## Files

| File | Change |
|---|---|
| `src/mind_meld/config.py` | Validate the local Grok usage bit and the Track 22B `type: "grok"` source without permitting a wider walk. |
| `src/mind_meld/cli.py` | Manage the scoped source and retained usage bit through one enable/disable verb and status copy. |
| `src/mind_meld/manifest.py` | Walk only hardcoded `skills/`, `commands/`, and `rules/`, rejecting child links and hard links. |
| `src/mind_meld/resolveflow.py` | Limit Grok conflict discovery to the same hardcoded directories. |
| `src/mind_meld/events_tail.py` | Gate Grok usage on the source or retained bit, dispatch warm safely, and preserve reader-scoped capture. |
| `src/mind_meld/host_usage.py` | Strict v1 reader, per-turn accounting, fingerprinted incremental cache. |
| `src/mind_meld/skills/retro_fleet/aggregator.py` | Consume latest accepted host snapshots and render coverage-aware family totals. |
| `docs/invariants/sync.md` | Record the hard-link and scoped Grok source boundary. |
| `docs/invariants/events-retro.md` | Record the Grok v1 reader, consent, cache, and consumer contracts. |
| `README.md` | Document opt-in Grok capture and update the former no-ledger caveat. |
| focused test modules above | Synthetic metadata-only fixtures and regression pins. |

## Out of scope

- A daemon, polling agent, server, cloud billing/API integration, or headless
  export command.
- Syncing `.grok` sessions, `config.toml`, credentials, logs, worktrees,
  prompts, tool output, or chat history. The shipped `grok` source walks only
  `skills/`, `commands/`, and `rules/`; see `docs/designs/host-parity.md`.
- A Codex/Grok `sessions-snapshot`, or uploading session transcripts. That is
  not a missed 18D task; see host-parity.md.
- Parsing `signals.json`, transcript streams, or context-window totals as a
  fallback.
- Historical cost estimates or billing reconciliation for Grok.
- Support for a future Grok storage shape without a new verified contract.
