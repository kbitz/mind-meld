# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

## Unprocessed

- [token-usage cleanup, ship] **DRY: extract `_merge_usage_bucket` helper.**
  The 4-key bucket-merge pattern `for k in ('input', 'cache_create',
  'cache_read', 'output'): target[k] += src.get(k, 0)` (plus the paired
  `by_model` walk) is duplicated at 6 sites across 3 modules:
  `token_usage._accumulate`, `token_usage.slice_window`,
  `events._aggregate_tokens_for_project`, `aggregator._merge_token_window`.
  Adding a 5th token field (e.g. cache_anthropic) requires touching all 6.
  Fix: extract `token_usage._merge_usage_bucket(target, src)` and
  `_merge_by_model(target, src)`. _Effort: S._

- [token-usage perf, ship] **Dirty-flag write skip in `lockedjson.locked_json_rmw`.**
  Cache is rewritten on every normal context exit, even when caller made
  zero mutations. On steady-state warm cache (~100% cache hits), every
  `mm push` pays the 320KB JSON serialization for no actual change.
  Within budget but wasted I/O on the hot path. Fix: track a `dirty` flag
  on `LockedJson`; default False; set True on caller mutation; skip
  `_write_json` when not set. Or hash the parsed dict on yield vs on exit.
  _Effort: S._

- [token-usage perf, ship] **`is_cache_cold` cheap stat-heuristic.**
  `is_cache_cold()` slurps and parses the entire (~320KB) cache JSON to
  check whether `files` dict is empty. Called from `_decide_token_walk_policy`
  on every push BEFORE acquiring the flock that re-parses the same bytes.
  Fix: replace with stat-based heuristic — file missing OR `st_size < 64`
  bytes treated as cold (well below any non-empty `{version, files: {...}}`
  payload). Saves the json.loads round-trip on the hot path. Documents
  the unlocked-read race in the docstring. _Effort: XS._

- [token-usage cleanup, ship] **DRY: cli.py token-cache lock+normalize block.**
  The `with locked_json_rmw(...) ... ljson.data["files"] = {}` normalize
  block is duplicated verbatim between `_run_events_tail` and
  `_run_events_backfill` (~30 lines each). Same shape, same defensive
  re-validation. Fix: extract a `token_usage.lock_and_get_files(on_contention)`
  context manager that yields the `files` dict directly, so cli.py call
  sites collapse to `with token_usage.lock_and_get_files("block") as files:`.
  Owner of the cache-shape invariants is then `token_usage.py`, not cli.py.
  _Effort: S._
