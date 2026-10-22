# Google Antigravity (`agy`) usage-source contract (census 2026-10-22)

**Host version: Antigravity 1.4.5** — bound to `host_usage.AGY_USAGE_CENSUS_HOST_VERSION` by `test_agy_contract_census_pin_matches_src_constant`.

Antigravity persists conversations in SQLite databases at `~/.gemini/antigravity-cli/conversations/<cid>.db`, accompanied by optional `-wal`, `-shm`, or `-journal` sidecars.

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
     - Subtag 7 (length-delimited string): `bot_id` (turn UUID, e.g. `bot-f53d78db-...`).
     - Subtag 9 (varint, optional): `thinking` tokens.
     - Subtag 10 (varint, optional): `response` tokens (`thinking + response == output` when present).

## Invariants & Refusal Gates

- **Step Types:** Only `step_type == 15` carries valid token usage. Rows with all-zero tokens or `bot_id` starting with `<synthetic>` are skipped. Any row with `step_type != 15` carrying non-zero tokens refuses the reader as `unsupported` (`token_usage_on_unexpected_step_type`).
- **Usage-less Skipped:** Any completed `step_type == 15` row (`status == 3`) that lacks tag 9 increments `usage_less_skipped`.
- **Deduplication:** Turns are deduplicated across databases by `sha256("agy\0" + bot_id)`. Conflicting usage data for the same `bot_id` refuses the reader as `malformed` (`bot_id_conflict`).
- **SQLite Safety:** Databases are opened with `mode=ro` and `immutable=1` when `-wal` is absent. `SQLITE_LIMIT_LENGTH` is capped at 16 MiB. Hot `-journal` (> 0 bytes) and pre-open vs post-close fingerprint drift refuse as `stale` (`hot_journal`, `identity_changed`).
- **Error Mapping:** Protobuf wire corruption, negative or overflow counters, or duplicate `bot_id` conflict refuse as `malformed`. Missing required tables or columns, unknown `model_enum`, or unexpected step type token usage refuse as `unsupported`.
