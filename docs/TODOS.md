# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

Single source of truth — there is no root-level `TODOS.md`. The two files were
reconciled on 2026-08-14; the root file's live inbox won and moved here, and the
`## Inbox` heading was renamed to `## Unprocessed` (what `/roadmap` drains).

## Unprocessed

### Deferred out of Track 16A (v0.12.21)

Each was surfaced by `/review` or `/ship`'s coverage audit and consciously NOT
fixed in that Track, because another Track already owns the surface.

- **`retention._gc_token_cache` and `_sweep_local_tmp_files` have zero tests.**
  Mutation-verified during Track 16A's review: making `_gc_token_cache` a no-op,
  and making `_sweep_local_tmp_files` return 0 without deleting, each left the
  whole suite green. Pre-existing (the code was untested inside `cli.py`), but
  CLAUDE.md now advertises both as routed invariant surfaces and **Track 17B is
  chartered to change their `--dry-run` semantics with nothing to regress
  against.** Write the tests before 17B starts, not after.
  **Priority:** P1. _Source: [ship] coverage audit 2026-08-15._

- **Three function-local imports in `resolveflow.py`.** `SYNCED_SUBDIRS` and
  `CONFLICT_V0_PREFIX` re-import `mind_meld.manifest` inside function bodies;
  they were cycle workarounds in `cli.py` and no cycle exists in the new module.
  Track 17E owns the thirteen-re-import sweep; these are the same shape.
  **Priority:** P3. _Source: [review] maintainability specialist 2026-08-15._

- **Three residual coverage gaps in Track 16A's new code**, all defensive or
  theoretical: an unreachable `OSError` branch in
  `skill_link._is_real_agent_dir_under_pytest`; `test_module_boundaries.py`'s
  `EXTRACTED` / `LEAVES` lists going stale silently (mitigated by the repo-wide
  cycle test); and the `"Console("` substring scan being evadable by
  `from rich.console import Console as C`.
  **Priority:** P4. _Source: [ship] coverage audit 2026-08-15._

- **Nine pre-0.11 releases have no `docs/PROGRESS.md` row** (0.10.3, 0.10.2,
  0.10.0, 0.9.6, 0.9.5, 0.8.8, 0.8.7, 0.8.6, 0.1.0). Found when the new
  CHANGELOG/PROGRESS parity gate ran for the first time. The gate enforces from
  0.11.0 forward and names these rather than hiding them; backfilling the prose
  is a separate call.
  **Priority:** P4. _Source: [ship] 2026-08-15._

The 61 items drained that day: 50 from `/full-review` 2026-08-14 and 11
pre-existing. Two `/full-review` findings became Hotfix Groups 13 and 14, 45 more
were placed into Groups 15–18, and 3 went to `## Future`. The 11
pre-existing items were each re-verified against live code — none done, none
overtaken — and removed from the inbox because all 11 were already duplicated in
`## Future`, which is why this inbox never emptied on the previous run.
