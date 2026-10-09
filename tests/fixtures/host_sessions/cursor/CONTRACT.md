# Cursor via Conductor usage contract (Track 67A)

Censused 2026-09-23 on one Mac. Cursor CLI: **2026.09.18-9a7762b**.
Persisted schema producer: **Conductor 0.87.3**, read from the installed
app's CFBundleShortVersionString and CFBundleVersion during implementation.
The run rows have no schema version. Both versions are pinned in host_usage.py;
pins document provenance, not compatibility with another release.

These are the fleet's first two Cursor sessions: three finished runs, two
sessions, one model (`grok-4.7`), one parameter combination (`fast=false`,
`reasoning_effort=high`). Total: **32,729,640 tokens**. Rows are real samples,
not hand-built substitutes. Redaction projects only runId, turnNumber, status,
model, usage, createdAt, startedAt and endedAt. Session directory names are
replaced; identifiers and counters are unchanged. No agents.ndjson (cwd and
blobEncryptionKey), result, error, events, checkpoints or transcripts ship.

Only `status == "finished"` with non-null usage contributes counters. The file
is rewritten in place, one line per runId: running/null becomes finished/usage.
Every stable read replaces that run's previous day/model/counter contribution.
The four counters are **disjoint**: inputTokens + outputTokens + cacheReadTokens
+ cacheWriteTokens equals totalTokens in all three samples. reasoningTokens
is a bounded subset of outputTokens, never an additional charge. The complete
turn belongs to endedAt's UTC date, even if it started before midnight.

Never observed: fast=true; composer-2.5/auto; nonzero cacheWriteTokens;
non-null usageRef; a midnight-spanning turn; a second producing Mac.
Observed later (2026-10-03, structure only): 10 `cancelled`, 1 `error` and
1 `queued` row across 7 of 16 run files, every one with null usage and no
usageRef. They contribute nothing; counters or a usageRef on them refuse.
Synthetic mutations in tests exercise these failure paths and
are explicitly not additional census evidence. Unknown statuses, missing
usage fields, invalid arithmetic and unknown shapes fail visibly. Nonzero
cache writes label the day partial; counters still publish with disjoint-v1
after the identity check, and an unpublished cache-write price remains unknown.
usageRef with null usage retains a partial placeholder. If its day has no
known token bucket, the reader refuses as partial, since the existing wire
would otherwise discard that warning. If the day has known usage, it publishes
with partial_sources. No synthetic zero tokens are invented.

The private cursor-host-tokens.json stores hashed run IDs, hashed requestId
aliases (v1.5.0), model IDs, UTC days and counters. It uses the shared
CACHE_VERSION and reader-owned
CURSOR_HOST_CACHE_RETENTION_DAYS = 90 (not events.CURSOR_SCAN_DAYS). Whole stable
files learned before a deadline commit, never a partially parsed file.
Repeated short passes are not guaranteed to converge: each rewritten file
must be reparsed. A larger read budget or attended warming can finish the scan.
The cache keeps runs removed by Conductor, whose retention is unknown. It
cannot recover runs pruned before mm first saw them. Temp-file fsync, atomic
rename and directory fsync protect the authoritative history; corrupt reads
refuse without replacing it. This bounds future loss, not historical backlog.

The original corpus covers **Cursor via Conductor**. Bare cursor-agent
persists no historical billing ledger; context-window token counts are not usage.
No Conductor store and no prior cache returns no_metadata_ledger; format drift
returns malformed/unsupported. Since Track 70A the SQLite `index.db` is read under
the open rule below; no content-bearing sibling (`agents`, `run_events`, the blob
tables, a per-agent `store.db`) is ever opened.

## Standalone completion census (2026-10-03)

CLI **2026.09.26-dd393fe** was probed outside Conductor through one interactive
Ask-mode run and headless Ask/Agent-mode runs. The interactive stop hook emitted
`model: grok-4.7-low`, input 17,534, cache read 1,152, cache write 0 and output
147. The installed CLI passes its inclusive turn counters to that hook, while
normalizing print-result usage to disjoint counters before emitting stdout.
The corresponding hook bucket is input 16,382, cache read 1,152 and output 147.
No context-window count was used to derive these figures.

Headless probes emitted result usage on stdout and sessionStart/sessionEnd
hooks, but no stop hook. Therefore native print-mode usage still needs the
`mm cursor-agent --print` wrapper. Interactive use can keep the original
`cursor-agent` command after `mm enable-source cursor` enrolls the stop hook.
Only future completed runs are measured. Earlier native sessions cannot be
recovered, and canceled/failed runs remain outside this completed-run contract.
Regression cases use redacted synthetic generation IDs and the observed hook
counters; they are not additional census evidence. No second producing Mac,
Fast run, nonzero cache write or Conductor/CLI overlap was measured here;
mutations exercise those branches without claiming a new census.

## Live follow-up (2026-10-04)

CLI **2026.10.01-e373342** (self-updated since the census) with Conductor
**0.90.1**, one Mac. Stop payload keys were unchanged. A two-turn interactive
Ask session (grok-4.7-low) produced one stop event per turn in the same
conversation, with distinct generation IDs:

| turn | input | cache read | cache write | output |
|---|---|---|---|---|
| 1 (~300 words) | 47,657 | 0 | 0 | 527 |
| 2 ("just OK") | 48,299 | 47,616 | 0 | 15 |

Counters are **per turn**, not cumulative (turn 2 output 15), and input is
inclusive: turn 2 = turn 1 context + its answer + the new prompt, so the
normalized input is 683. Cursor resolves the hook's bare `mm` from its own
environment (the installed pipx `mm`), not the launching terminal's PATH.

A Cursor turn run through Conductor 0.90.1 fired **no** user-level stop hook,
so Conductor runs and hook captures do not overlap; request-ID dedup is
defensive. Conductor 0.90.1 no longer writes `runs.ndjson`: the new workspace
store is SQLite (`index.db` with a `runs` table carrying `request_id`,
`status` `FINISHED`, `model`, `model_params_json`, disjoint `usage_json`
and ISO `finished_at`; per-agent `store.db` holds blobs). Track 70A reads that
`index.db`; the SQLite census below records what was measured.

Prices are API **list-rate equivalents**, not spend on the censused Cursor
Ultra subscription. Cursor's rates were fetched 2026-09-23:
https://cursor.com/docs/models-and-pricing and
https://cursor.com/docs/models/grok-4-7. Standard Grok 4.7 is $2 input,
$0.50 cache read and $6 output per million tokens. Above 256k input per request
those rates double; per-turn totals cannot recover that tier, so cost is a
floor. Cache-write price is unpublished, not demonstrated to be zero.

Fast mode bills at 2x standard, or 3x standard for long context, and is the
default on Pro and higher. Its tokens use the deliberately unpriced
grok-4.7-fast ID. Fast-only rows show cost `—`; mixed rows show `≥` for the
priced subtotal, omitting Fast cost entirely. Effort has no separate published
rate. auto/composer remain Unclassified; adding a family is deferred until
real use is observed. Grok Build history remains complementary, not redundant.

## SQLite census, 2026-10-09, Conductor 0.90.1

Pin: `CURSOR_SQLITE_CENSUS_CONDUCTOR_VERSION = "0.90.1"` in host_usage.py, bound to
this heading by `test_census_pin_matches_the_contract`. The JSONL pins above are
unchanged, and like them this pin documents provenance, not compatibility with
another release. Measured on one Mac (device 889e42c0, mm HEAD 98e9a69), Conductor
unchanged since 2026-10-02. Two workspace stores under
`~/Library/Application Support/com.conductor.app/cursor-sdk-store/`; each is a
directory named `sha256(workspaceDir)[:16]`, so only a `[0-9a-f]{16}` directory is
a SQLite store.

**Recipe.** Both stores held `index.db`, `index.db-wal` and `index.db-shm`, so each
was opened `mode=ro` with `PRAGMA query_only=ON`, one snapshot, reading only
`sqlite_master`, `table_info(runs)` and metadata columns of `runs`. `ls -l` of the
three files before and after was identical (size and mtime to the minute): the
census read left no visible footprint.

| store | `index.db` | `index.db-wal` | `index.db-shm` |
|---|---|---|---|
| `6a0374c100b4e8c8` | 950,272 B | 152,472 B | 32,768 B |
| `a12bae7f256784c5` | 4,096 B | 465,592 B | 32,768 B |

`sqlite_master`: tables `agents`, `run_events`, `runs`; indexes
`run_events_idempotency_key_idx`, `sqlite_autoindex_agents_1`,
`sqlite_autoindex_run_events_1`, `sqlite_autoindex_run_events_2`,
`sqlite_autoindex_runs_1`, `sqlite_autoindex_runs_2`. `table_info(runs)` is 19
columns, `run_id` the primary key: `run_id`, `request_id`, `agent_id` (NOT NULL),
`turn_number` (INTEGER NOT NULL), `status` (NOT NULL), `model`, `model_params_json`,
`start_checkpoint_ref_json`, `latest_checkpoint_ref_json`, `error_code`, `usage_ref`,
`usage_json`, `result`, `created_at` (NOT NULL), `updated_at` (NOT NULL),
`started_at`, `finished_at`, `cancelled_at`, `expired_at`. All are TEXT but
`turn_number`.

Status and counter counts: store `6a0374c100b4e8c8` held 4 `FINISHED` rows, all 4
with `usage_json` and none with `usage_ref`; store `a12bae7f256784c5` held 1
`CANCELLED` row with neither. No `request_id` was shared by two rows. Rows below are
projected to the selected metadata columns; ids and counters are unchanged. No
`result`, `error_code`, checkpoint, `agents` or `run_events` value was read.

| run_id | request_id | status | terminal stamp (= `updated_at`) | usage_json |
|---|---|---|---|---|
| run-16dccfa8-3188-47e6-a329-b0bae16ef858 | b918e504-2fad-48fb-8745-022e99e99928 | FINISHED | `finished_at` 2026-10-09T12:05:35.088Z | in 38,431 · out 1,389 · cacheRead 22,912 · cacheWrite 0 · total 62,732 · reasoning 1,019 |
| run-45353a09-b57a-4908-8f74-4aa52aacd727 | 2ae366f2-1777-4b40-b5ec-eab4a1fb48f4 | FINISHED | `finished_at` 2026-10-09T12:06:52.427Z | in 155,198 · out 3,727 · cacheRead 68,608 · cacheWrite 0 · total 227,533 · reasoning 1,342 |
| run-5bc7a28a-5395-4c47-b290-24aeea14dedc | ffbf493d-e1fc-4b51-af77-c85532b04509 | FINISHED | `finished_at` 2026-10-09T12:16:24.500Z | in 115,692 · out 5,094 · cacheRead 110,848 · cacheWrite 0 · total 231,634 · reasoning 3,400 |
| run-52d56731-c1c6-4b6e-8100-797dc2bdcf96 | 727d14ee-98d2-4132-9811-aa2551bd2c5a | FINISHED | `finished_at` 2026-10-09T12:23:45.204Z | in 67,571 · out 1,183 · cacheRead 7,936 · cacheWrite 0 · total 76,690 · reasoning 65 |
| run-7a845278-9092-4f69-931e-b1f9388bc0d6 | 6ebf9abe-139e-4946-8c36-e4ecf5a07336 | CANCELLED | `cancelled_at` 2026-10-09T12:04:54.455Z | NULL (no `usage_ref`) |

All four counted rows total **598,589 tokens**. The counters are disjoint
(`input + output + cacheRead + cacheWrite == totalTokens` in every row) and
`reasoningTokens`, present in all four, is a subset of output. `usage_json` is
`typeof` text on the finished rows and NULL on the cancelled one. Timestamps are
`toISOString()` text, `YYYY-MM-DDTHH:MM:SS.mmmZ`. Every row's model is `grok-4.7` with
`model_params_json` `[{"id":"fast","value":"false"},{"id":"reasoning_effort","value":"xhigh"}]`.
The legacy `runs.ndjson` rows are keyed `runId` and dated by `endedAt` in
milliseconds; a SQLite run is dated by its own terminal stamp, and an `ERROR` run
by `updated_at`, which has no terminal stamp of its own.

**Producer source only, not census.** These come from Conductor 0.90.1's own code
and were never observed in a store: counters on `CANCELLED`, `ERROR` and `EXPIRED`
rows; a `usage_json` that arrives after the row finished; `usage_ref` and how it
resolves (the producer source shows no resolution: **unknown**); the `CREATING`,
`EXPIRED` and `ERROR` statuses; a missing `reasoningTokens` (the property is
optional); rows migrated from `runs.ndjson` (the migration force-deletes the file);
and a pre-ALTER store lacking a current column. Tests exercise each by mutation and
are not census evidence. The producer opens the store with WAL, `busy_timeout 5000`
and `BEGIN IMMEDIATE` writes, and 0.90.1 is unchanged since 2026-10-02.

**Observed with the branch reader (2026-10-09, this Mac).** Conductor was not
running and no process held the stores open: the `-wal` files were 0 bytes
(`index.db` 958,464 B and 61,440 B) and the `-shm` files had been left behind. A
control of 8 seconds with no mm read changed nothing. Each `mode=ro` read by mm then
advanced both `-shm` files' mtime while their size and SHA-256 stayed identical;
`index.db` and `-wal` were untouched. With a live writer holding a non-empty WAL
(a synthetic two-process probe) the `-shm` content changed (SQLite's read marks) but
its mtime did not. The `-shm` is SQLite's own wal-index, so the open rule exempts it
beyond (dev, ino, type); a reader that avoided even that would need `immutable=1`
against a WAL that a live writer might extend, which this adapter does not do.

**Attended follow-up (2026-10-09, Conductor 0.90.1).** Two user-interrupted
`grok-4.7` turns in store `ef1e666541566c2e` ended at 18:46:58.200Z and
18:47:21.769Z. Both were `CANCELLED` with NULL `usage_json`, distinct `run_id` and
`request_id` values, and `updated_at == cancelled_at`. The first attempt showed
no answer text before interruption; the second showed agent activity. This
attempt did not produce a counter-bearing cancelled row, so that case remains
producer-source only, not census.

After the user quit Conductor, `ls -l index.db*` at 18:49:13Z showed:

| store | `index.db` | `index.db-wal` | `index.db-shm` |
|---|---|---|---|
| `6a0374c100b4e8c8` | 958,464 B | 0 B | 32,768 B |
| `a12bae7f256784c5` | 61,440 B | 0 B | 32,768 B |
| `ef1e666541566c2e` | 495,616 B | 0 B | 32,768 B |

No Conductor process was found. The quit-state capture listed files without
opening SQLite; the new store's WAL had been 708,672 B before quitting. The
original four counted rows and their 598,589-token total were unchanged.

### Open rule (minimal footprint)

`<store>/index.db` is opened through `Path.as_uri()` with a query string. Every
listed file is first lstat-ed and must be a regular, non-symlink file; otherwise
the read refuses (`io_error`/`non_regular_path`) and no file is opened. The
`-journal` is type-checked before its size is looked at.

| `-wal` | `-shm` | `-journal` | Action |
|---|---|---|---|
| absent | absent or present | absent or 0 B | `mode=ro&immutable=1`; identity (dev, ino, size, mtime_ns) of `index.db` and `-wal` absence, before and after; a change is `stale/identity_changed` |
| present, > 0 B | present | absent or 0 B | `mode=ro`; identity (dev, ino, type) of the three files |
| present, 0 B | present | absent or 0 B | `mode=ro` (a live connection may have truncated the WAL) |
| present | absent | absent or 0 B | `mode=ro`; the `-shm` SQLite creates is exempt from the post-read check |
| any | any | non-empty | `stale/hot_journal` |

Footprint-free is guaranteed only in steady states. A close race or a crash can
leave SQLite-valid `-wal`/`-shm` files, which Conductor recovers on its next open;
this departs from the codex-memory owned-tree rule and is disclosed in
events-retro.md and the README. mm never writes, checkpoints or copies a store
file, never opens a per-agent `store.db`, and never selects `result`, `error_code`
or any `agents`/`run_events`/blob column. The same-user race between lstat and open
is accepted; the legacy JSONL reader skips a non-regular ledger instead of refusing.

The local cache's `sqlite_retained` hash list preserves SQLite counter retention
when a store prunes a row, drifts, disappears or is not reached before a failed
pass. Stale JSONL empty/placeholder copies cannot erase those counters. An older
mm may drop this additive field; it cannot preserve that guarantee. The per-run
cache and synced event shapes are unchanged.

**Re-check recipe** (run by hand after a Conductor update; it only reads). Pick the
mode from fresh filesystem state, refuse non-regular files and a non-empty
journal, report `CANTOPEN` as-is and never retry in another mode:

```sh
S="$HOME/Library/Application Support/com.conductor.app/cursor-sdk-store/<id>"
ls -l "$S"/index.db*
python3 -I - "$S" <<'PY'
import sqlite3, stat, sys
from contextlib import closing
from pathlib import Path

store = Path(sys.argv[1])
files = {}
for name in ("index.db", "index.db-wal", "index.db-shm", "index.db-journal"):
    try:
        info = (store / name).lstat()
    except FileNotFoundError:
        if name == "index.db":
            raise
        continue
    if not stat.S_ISREG(info.st_mode):
        sys.exit(f"{name} is not a regular file")
    files[name] = info
if "index.db-journal" in files and files["index.db-journal"].st_size:
    sys.exit("index.db-journal is non-empty; stop and let Conductor recover it")
mode = "mode=ro" if "index.db-wal" in files else "mode=ro&immutable=1"
with closing(sqlite3.connect((store / "index.db").as_uri() + "?" + mode, uri=True)) as conn:
    conn.execute("PRAGMA query_only=ON")
    for row in conn.execute("PRAGMA table_info(runs)"):
        print(row)
PY
ls -l "$S"/index.db*
```

`index.db` and `-wal` must remain unchanged. SQLite may update `-shm` read marks
or its mtime, or create `-shm` if absent; an existing `-shm` must keep its identity.
The README's verify block ([Conductor SQLite
stores](../../../../README.md#cursor-sqlite-stores)) covers the end-to-end check.

### Retirement and alternatives

**Retirement default.** On the next Conductor store change, retire the SQLite
adapter (status reverts to "not counted"). The exception: Conductor Cursor runs on
3 or more distinct days in the prior 30 days, fleet-wide; then the user decides.

**Dead ends and alternatives**, each "fetched 2026-10-09 by /autoplan web search,
community/forum sources, not verified by mm":

- `conductor.db` holds only `sessions.context_token_count` (a local probe, a context
  gauge rather than billing).
- ccstats reads Cursor's state-database `tokenCount`, also a context gauge:
  <https://docs.rs/crate/ccstats/0.3.0>
- There is no individual-plan usage API:
  <https://forum.cursor.com/t/usage-api-cli-command/160967>
- The Teams/Enterprise Admin API `POST /teams/filtered-usage-events` needs a team
  admin key: <https://forum.cursor.com/t/how-to-obtain-token-usage-per-request/168317>
- ACP returns no usage:
  <https://forum.cursor.com/t/cursor-acp-doesn-t-seem-to-return-token-usage-in-promptresponse-usage/160395>
- **Evaluated alternative:** the cursor.com dashboard's per-event CSV export,
  <https://forum.cursor.com/t/dashboard-export-usage-events-csv-no-longer-exports-cost/167193>.
  It is manual, has no `run_id` and needs no mm import. It is the long-term path if
  Cursor work leaves Conductor.
