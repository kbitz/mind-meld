# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

## Unprocessed

- **cli.py diff-call-site DRY pass (post-Track-2A followup)** [plan-eng-review 2026-04-23] — `cli.py:1454-1459, 1843-1868, 2101-2112, 2464-2473`: the four `diff_files` call sites share a per-source iteration pattern but diverge on filtering (push filters by `has_changes`, status/diff filter by `--source` arg, pull builds local_files by hashing). Track 2A's `_pull_core` decomposition resolves one of the four; the other three (push, status, diff) still carry the boilerplate. Candidate primitive: a helper that takes local + remote source dicts and yields `(src_name, src_data, remote_src, diff)` tuples, callers filter. **Why:** boilerplate that rots — easy to silently diverge between sites during later edits. **Pros:** one change point, better for future diff-semantics tweaks. **Cons:** a small abstraction with four call sites, one of which has a filter that doesn't fit. **Context:** flagged during Track 1B /plan-eng-review; recommended to defer until after Track 2A lands (_pull_core decomposition changes the shape of one call site). **Depends on:** Track 2A.

- **GC: validate blob shape, not just depth** [codex-adversarial 2026-04-23]
  `_do_gc` (cli.py:2660-2698) now flags wrong-depth `data/` entries (Track 1A
  Group 1 fix), but a 3-segment path with bogus middle/leaf still gets reaped
  as an "orphan" if not in `referenced_hashes`. Examples: `data//foo.enc`
  (empty device_id), `data/dev/not-a-sha.enc` (non-hex leaf). Add a stricter
  validator: device_id segment matches `[0-9a-f]{8,}`, leaf matches `[0-9a-f]{64}`.
  Pre-existing risk surface (not introduced by Track 1A) but worth closing as
  follow-up to the depth-validation work. _src/mind_meld/cli.py, ~20 lines._ (S)

- **Autopull breadcrumb: `degraded` outcome on durability fsync failure** [codex-adversarial 2026-04-23]
  `_pull_core` (cli.py:1978-1990) warns to stderr when `fsutil.fsync_dir` fails
  during the deferred-durability commit (Track 1A Group 1 quiet-audit fix), but
  the result still has no `durability_degraded` field, so `autopull` writes
  `outcome: "success"` to the breadcrumb. A user reading `mm status` only sees
  "success" while recently-pulled renames may not survive crash/power loss.
  Mirrors the autopush "no-sources" breadcrumb fix: thread `durability_degraded`
  through `PullResult`, surface as `outcome: "degraded"` in autopull. _src/mind_meld/cli.py, ~25 lines._ (S)
