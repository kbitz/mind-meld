# Cursor account usage contract (Track 107A)

**STATUS: censused 2026-11-04 and 2026-11-05 on one Mac (Mac Studio, Mac14,13), cursor-agent
`2026.10.22-27dcf5f`, no Linux machine reachable.** The census ran mm's own reader code against
the owner's real login and recorded names and counts only; no token or token file was opened,
printed or copied. Login stores come from steipete/CodexBar's public Cursor provider (MIT;
`docs/cursor.md`, `CursorAppAuth.swift`). The billing route was a lead from the owner (the
dashboard's export button); everything below it was measured. The synthetic file beside this one
(`billing.csv`) follows the measured shape, holds no real data, and is not census evidence. It
contains no login-shaped strings.

## Source of the usage: the billing-items export

`GET https://cursor.com/api/dashboard/v2/export-billing-items-csv?scope=user&startTime=<ISO>&endTime=<ISO>`
with the cookie `WorkosCursorSessionToken=<userId>%3A%3A<jwt>` (`userId` = the last `|` part of the JWT
`sub`, `[A-Za-z0-9._-]` only). `startTime` and `endTime` are ISO 8601 UTC (`2026-11-01T00:00:00.000Z`,
colons percent-encoded); the window is `[start, end)` and one UTC day is `00:00Z` to the next `00:00Z`.
A read-only GET; mm sends nothing else (no `Origin`, no body).

| Fact | Result |
|---|---|
| Columns | `Billing Item ID`, `Event Time`, `Product`, `Charge Category`, `Usage Type`, `Principal Type`, `Principal ID`, `Usage`, `Pricing Currency`, `Pricing Contracted`, `Model`, `User Email` |
| Grain | one row per billed model call (not per conversation turn: ~1,000 rows a day against ~40 per-event rows on the same days), `Billing Item ID` unique on 22,625 of 22,625 rows |
| Window measured | `startTime` 2026-10-06, `endTime` 2026-11-06: 22,625 rows, one principal, one e-mail domain (`cnyfeeds.com`, the owner's), 30 days 2026-10-06 .. 2026-11-04 |
| `Product` | `cursor` 22,624, `grok-chat` 1 (a `grok-4.20` row whose `Usage` uses other key names: not Cursor, ignored) |
| `Charge Category` / `Principal Type` / `Pricing Currency` | `usage` / `user` / `USD` on every row |
| `Usage Type` | `included` 22,623, `on_demand` 2 |
| `Usage` | JSON object of string counters. Keys on Cursor rows: `prompt-text-tokens`, `cached-prompt-text-tokens`, `completion-text-tokens`, and `cache-write-text-tokens` (13,121 of the rows); `{}` on 28 rows (a call that carried none) |
| `Event Time` | ISO 8601 UTC with milliseconds and `Z` |
| Models (Cursor rows, 2026-10-05 .. 11-04) | `grok-4.7-xhigh-fast`, `grok-4.7-high-fast`, `grok-4.7-xhigh`, `grok-4.7-high`, `grok-4.7-medium`, `claude-opus-5-5-medium`, `cursor-grok-4.6-high-fast` |
| Coverage | every day through the current one, including after 2026-10-21: 6M to 11M prompt tokens a day after the plan moved |
| Latency | erratic: one day 4.1 s, 20.6 s and 28.5 s on repeat; three days about 5 s to 20 s; 30 days 66 s and 155 s; a cold fill of 30 days in ten 3-day requests took 51 s; a warm cache costs 24 ms |
| `grok.com` | `https://grok.com/dashboard/api/dashboard/v2/export-billing-items-csv` answers 403 to the Cursor session. mm never calls it and never looks for a grok.com or x.com login |

### Counters

- Disjoint. `cached-prompt-text-tokens` exceeds `prompt-text-tokens` on 21,445 rows, so the prompt count excludes the
  cached part. Mapping: `prompt-text-tokens` input, `cache-write-text-tokens` cache creation,
  `cached-prompt-text-tokens` cache read, `completion-text-tokens` output.
- Against the per-event endpoint (`POST /api/dashboard/get-filtered-usage-events`, which lists requests with
  `tokenUsage`), the reader's own parsed daily totals are **identical per model and per counter on all 16 days both
  cover** (2026-10-05 .. 2026-10-20; checked 2026-11-05 against the reader's cache). 2026-10-21 differs only because the
  per-event list stops at 04:05Z that day.

### Why the per-event endpoint is not used

The per-event endpoint (`get-filtered-usage-events`, `page`/`pageSize`/`startDate`/`endDate`, response
`totalUsageEventsCount` + `usageEventsDisplay`, events with `usageEventId`) lists 643 events 2026-09-22 .. 2026-10-21 and
nothing later, under every request variant tried (future `endDate`, integers instead of strings, no dates, `startDate` 0,
`pageSize` 100 with pages 1 and 2). The account moved from the legacy Ultra plan to the unified X/Cursor plan then
(`usage-summary` reports `membershipType` `unified_ultra`, cycle from 2026-11-01, monthly plan `used` 0;
`v2/usage-status?scope=user` reports a `weeklyIncludedUsage` with `usedCents` above 0, a cents aggregate with no tokens).
`get-aggregated-usage-events` (POST, per-model over a range, a lead from Raycast's cursor-costs) answered an HTTP error to
the shape tried. `GET /api/dashboard/teams` answered 405. The `dashboard` and `settings` pages redirect to a web-session
bootstrap and were not followed.

## Login sources (read-only, in memory only; first match wins)

| Platform | Source | Read |
|---|---|---|
| macOS | Cursor.app `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` | SQLite `file:...?mode=ro` (`&immutable=1` when no `-wal`/`-shm` exist), `SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'`; TEXT, or BLOB decoded as CodexBar does (ASCII UTF-16LE, else UTF-8) |
| macOS | cursor-agent's login in the Keychain (generic password) | the system `security` tool's `find-generic-password` with service `cursor-access-token` and account `cursor-user`, value to stdout |
| Linux | `$XDG_CONFIG_HOME/cursor/auth.json` (absolute value, else `~/.config`) | JSON field `accessToken` |
| Linux | `.../Cursor/User/globalStorage/state.vscdb` under the same config home | as macOS |

- Census on this Mac: Cursor.app's database is absent (the owner does not use the app), the Keychain item is the source,
  and `KEYCHAIN_PREFIX = "cursor"` is confirmed (cursor-agent derives account `<prefix>-user` and service
  `<prefix>-access-token` from one prefix, read from its code and confirmed by the item existing). The read returned a
  JWT-shaped value with no prompt on this Mac.
- A plain JWT in the app database needs no Keychain access. A value prefixed `cursor-safe-storage:` is encrypted; mm does not
  decrypt it (cause `login_encrypted`; `docs/TODOS.md` holds the item and its trigger).
- The JWT is used only while its `exp` is more than 60 s away (`login_expired` otherwise); mm never refreshes or writes it.
  Its claims are `iss` `https://authentication.cursor.sh`, `aud` `https://cursor.com`, `type` `session`, `sub`
  (`auth0|user_...`), `exp` (about 60 days after minting), `time`, `randomness`, `scope`. The account key is the hash of the
  `sub` user id, the same id `GET /api/auth/me` returns.
- The Linux auth file and the Cursor.app paths are CodexBar's, not mm's evidence.

## Identity (2026-11-05)

`GET /api/auth/me` keys: `automation_client` (bool), `created_at`, `email`, `email_verified` (bool), `id` (int), `name`,
`picture`, `sub`, `updated_at`. Its e-mail domain (`cnyfeeds.com`) is the owner's, `owningUser` equals its `id` on 643 of 643
per-event rows, and `automation_client` is false: the account the reader sees is the owner's own. mm no longer calls it; the
account key comes from the JWT.

## Per-request identifier

Per-event rows carry `usageEventId` (a UUID, 643 of 643 unique); billing rows carry `Billing Item ID` (22,625 of 22,625
unique). Neither joins to the local Cursor readers' keys (0 of 83 hashed keys matched `usageEventId` or `conversationId`), and
neither is published. This does not establish inclusion: replacement is disabled unless every local run on a day is proven to belong to the account export.

## Open after the census

- Whether the account's usage includes the local Conductor and hook runs cannot be proven by id; the local share is tiny
  (633,390 tokens of 1.42B on 2026-10-06 .. 11-04), so the worst-case undercount of day replacement is that share.
- No Linux machine was reachable: the Linux login paths have no live evidence.
- The export's latency is the Track's operational risk: a cold cache fills over several attended pushes (20 s each) or in one
  long call; `mm status` shows `account usage through <day>` while it fills.


## Review inclusion census (2026-11-05 UTC; owner-requested)

Read the existing mm Cursor cache without changing it, then fetched the billing export for
its two days overlapping the reader's finished-day lookback. Credential and CSV remained in
memory; only these counts were retained. No live push or fleet-store write was performed.

| Measure | Count |
|---|---:|
| Local cached runs | 83 |
| Runs with time-of-day metadata | 0 |
| Overlapping finished days | 2 |
| Local runs on those days | 5 |
| Cursor billing rows on those days | 1,583 |
| Same-day model plus exact four-counter matches | 0 / 5 |
| Small-window timestamp matches established | 0 |

The first overlapping day had 4 local runs and 975 billing rows; the second had 1 local run
and 608 billing rows. The relaxed necessary check used the same UTC day, model (including
local Grok effort-suffix normalization) and exact disjoint input/cache-create/cache-read/output
counters, consuming each matched row at most once. Even that check found zero matches.
It was not a timestamp-based proof: the cache retains only `day`, `model`, `usage`, and
`partial`. Local runs can aggregate multiple model calls, so a row mismatch also does not
prove absence from the account. No production verified days were enabled. Account-only
counting could omit local-only usage; adding both could double-count. Owner decision required.

The local day cache is now bound to the hashed account identity; a login change or unbound
legacy cache drops old totals and completion metadata before refetching, including on failure.
The 20-second opt-in reader exception is owner-approved (2026-11-04), documented with the
capture budget; a warm cache makes no request and autopush never selects this reader.
