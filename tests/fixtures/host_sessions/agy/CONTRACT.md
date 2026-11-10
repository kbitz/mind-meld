# Google Antigravity (`agy`) usage-source contract (census 2026-11-09)

**Host version: Antigravity 1.5.3** — bound to `host_usage.AGY_USAGE_CENSUS_HOST_VERSION` by `test_agy_contract_census_pin_matches_src_constant`. First census: 2026-10-22 (Antigravity 1.4.5).

Antigravity persists conversations in SQLite databases at `~/.gemini/antigravity-cli/conversations/<cid>.db`, accompanied by optional `-wal`, `-shm`, or `-journal` sidecars.

## Census (2026-11-09, Antigravity 1.5.3)

Counts-only read over 208 `.db` files (one `steps` schema, 0 open errors, step timestamps spanning 2026-11-06 to 2026-11-10 UTC; no transcript text, paths or identifiers read):

| step_type | rows | DBs | statuses | rows with tag 9 | rows with nonzero tokens |
|---|---|---|---|---|---|
| 14 | 222 | 201 | 3: 220, 5: 2 | 0 | 0 |
| 15 | 8,226 | 200 | 3: 7,806, 5: 420 | 8,226 | 8,226 |
| 17 | 12 | 6 | 3: 12 | 0 | 0 |
| 23 | 129 | 39 | 3: 121, 5: 8 | 129 | 129 |
| 101 | 195 | 37 | 3: 185, 5: 10 | 0 | 0 |
| 132 | 12,864 | 66 | 2: 1, 3: 12,007, 5: 673, 6: 12, 7: 171 | 0 | 0 |

- Step 23's `TokenUsage` (tag 9) shares step 15's subtag set: 1, 2, 3, 5, 6 (varint), 7, 8, 11 (length-delimited), 9, 10 (varint); subtag 4 (`cache_create`) is absent in all observed rows.
- Every step 23 row has a timestamp, a non-`<synthetic>` `bot_id`, a model enum present in `gen_metadata`, and satisfies `thinking + response == output` (129 of 129).
- `bot_id`s are disjoint across step types (8,226 unique on step 15, 129 unique on step 23, 0 shared across types or repeated within a type).
- Subtags 6 (varint), 8 and 11 (length-delimited) occur in `TokenUsage` on both step 15 and step 23 and are ignored by the reader.

## SQLite Schema & Protobuf Wire Encoding

1. **`gen_metadata` table:**
   Columns: `idx` (INTEGER PK), `data` (BLOB), `size` (INTEGER).
   `data` contains a Protobuf message:
   - Tag 1 message:
     - Subtag 3 (varint): `model_enum` integer identifier.
     - Subtag 19 (length-delimited string): `model_slug` (e.g. `gemini-4-argon`, `gemini-3.8-flash`).

2. **`steps` table:**
   Columns: `idx` (or `step_index`) INTEGER PK, `step_type` INTEGER, `status` INTEGER, `metadata` BLOB.
   `metadata` contains a Protobuf message:
   - Tag 1 message:
     - Subtag 1 (varint): UTC epoch timestamp in seconds.
   - Tag 9 message (`TokenUsage`):
     - Subtag 1 (varint): `model_enum`.
     - Subtag 2 (varint): `input` tokens (disjoint).
     - Subtag 3 (varint): `output` tokens (inclusive of thinking).
     - Subtag 4 (varint, optional): `cache_create` tokens (default 0; non-zero marks day partial).
     - Subtag 5 (varint, optional): `cache_read` tokens (default 0).
     - Subtag 6 (varint, optional): present, ignored.
     - Subtag 7 (length-delimited string): `bot_id` (turn UUID, e.g. `bot-f53d78db-...`).
     - Subtag 8 (length-delimited, optional): present, ignored.
     - Subtag 9 (varint, optional): `thinking` tokens.
     - Subtag 10 (varint, optional): `response` tokens (`thinking + response == output` when present).
     - Subtag 11 (length-delimited, optional): present, ignored.

## Invariants & Refusal Gates

- **Step Types:** `step_type` 15 and 23 carry token usage and are counted alike. A row with no tag 9, all-zero tokens, or `bot_id` starting with `<synthetic>` is skipped. Any other `step_type` carrying non-zero tokens refuses the reader as `unsupported` (`token_usage_on_unexpected_step_type`).
- **Usage-less Skipped:** Any completed `step_type` 15 or 23 row (`status == 3`) that lacks tag 9 increments `usage_less_skipped`.
- **Deduplication:** Turns are deduplicated across databases by `sha256("agy\0" + bot_id)`. Conflicting usage data for the same `bot_id` refuses the reader as `malformed` (`bot_id_conflict`).
- **SQLite Safety:** Databases are opened with `mode=ro` and `immutable=1` when `-wal` is absent. `SQLITE_LIMIT_LENGTH` is capped at 16 MiB. Hot `-journal` (> 0 bytes) and pre-open vs post-close fingerprint drift refuse as `stale` (`hot_journal`, `identity_changed`).
- **Error Mapping:** Protobuf wire corruption, negative or overflow counters, or duplicate `bot_id` conflict refuse as `malformed`. Missing required tables or columns, unknown `model_enum`, or unexpected step type token usage refuse as `unsupported`.
