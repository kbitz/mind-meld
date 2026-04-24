# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

## Unprocessed

- **cli.py diff-call-site DRY pass (post-Track-2A followup)** [plan-eng-review 2026-04-23] — `cli.py:1454-1459, 1843-1868, 2101-2112, 2464-2473`: the four `diff_files` call sites share a per-source iteration pattern but diverge on filtering (push filters by `has_changes`, status/diff filter by `--source` arg, pull builds local_files by hashing). Track 2A's `_pull_core` decomposition resolves one of the four; the other three (push, status, diff) still carry the boilerplate. Candidate primitive: a helper that takes local + remote source dicts and yields `(src_name, src_data, remote_src, diff)` tuples, callers filter. **Why:** boilerplate that rots — easy to silently diverge between sites during later edits. **Pros:** one change point, better for future diff-semantics tweaks. **Cons:** a small abstraction with four call sites, one of which has a filter that doesn't fit. **Context:** flagged during Track 1B /plan-eng-review; recommended to defer until after Track 2A lands (_pull_core decomposition changes the shape of one call site). **Depends on:** Track 2A.
