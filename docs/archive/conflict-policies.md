> SUPERSEDED on 2026-10-19 by [current project spec](../SPEC.md). Historical
> rationale only; not a source of current scope, scheduling or release policy.

# Conflict policies, three-way merge and the resolver

> Written 2026-10-09/10 against `98e9a69` (v1.6.0). Not yet admitted to the
> roadmap. Builds on [the 2.0 store](../designs/store-v2.md), which owns the causal
> model: version vectors, the ledger, fast-forwards, symmetric resolution and
> `displaced`. Ships as 2.1 and 2.2.
>
> **Maintainer decision (2026-10-09):** prefer the most correct design;
> config keys and conflict UX may change in these releases.

## Outcome

After 2.0, a conflict means two machines really changed the same file.
2.1 lets each path declare what that should do:
- generated files never sync;
- append-only logs union;
- text gets a real three-way merge;
- latest-state files take the newest version;
- only what remains waits for a person.

Nothing is lost: every version a policy replaces is in `displaced` for 30
days.

## Why

- **Most conflicts today are not conflicts.** mm has no last-synced base, so
  any peer edit to a file this machine never touched is classified as a
  conflict (`cli.py:2006-2021`). That is why picking the newer copy is
  almost always the right answer. The 2.0 store's fast-forward rule removes this whole
  class; see [store-v2 §4](../designs/store-v2.md#4-causal-versions).
- **Most of the rest are files no human wrote.** v0.12.51's forensic pass
  over 88 recorded conflicts found 53% were generator output, and 64%
  including machine-written session state. Exclusion converged every one
  ([sync invariants](../invariants/sync.md#generated-files-are-not-sync-data-load-bearing-v01251)).
  Per-path policies generalize that one lever into five.
- **The rest deserve better than a whole-file pick.** With the base known
  (2.0 ledger plus the `base` table below), non-overlapping edits merge the
  way git merges them. `lcs_merge`'s synthetic ancestor (`merge.py:183-196`)
  existed only because no base was available.
- **Not the cancelled auto-resolver.** The conflict invariants forbid the
  conflict-decision collector and its learned similarity resolver, whose
  output was fixed by construction
  (`conflicts.md#collector-removal-and-auto-resolver-cancellation`). Policies
  are deterministic rules the user declares; nothing is learned. Record this
  distinction when C2 lands.

## Policies

Policies apply only to **concurrent** versions; everything else is a
fast-forward. They are declared as one ordered rule list per source: first
match wins, then built-in defaults. The list replaces `exclude_patterns`:

```toml
[[sync.sources]]
name = "gstack"
path = "~/.gstack"
include_dirs = ["projects", "analytics", "retros"]
rules = [
  ["projects/*/brain-cache/*", "local"],
  ["projects/*/checkpoints/*", "newest"],
  ["greptile-history.md",      "union"],
]

[sync]
conflict_fallback = "newest"   # or "ask"
```

| Policy | Meaning | Built-in default for |
|---|---|---|
| `local` | Never published or pulled. Switching a path to or from `local` authors no tombstones, the same as today's exclusion invariant. | every current `exclude_patterns` glob, including P1's generator trees; generated host skills |
| `union` | Line-set union in deterministic order (`merge_jsonl`, `merge_lines`) | `*.jsonl` |
| `merge` | Three-way merge against the real base. A clean merge applies; overlapping hunks go to the fallback. | other text, including `MEMORY.md` |
| `newest` | Higher mtime wins; ties go to the higher sha | binary |
| `ask` | Hold the remote version in `conflicts` for `mm resolve` | none by default |

- **Fallback.** `conflict_fallback` covers what a policy cannot finish:
  overlapping hunks, a missing base, or binary content under `merge`. Its
  default is `newest`, matching current practice, and `displaced` makes that
  lossless.
- **Symmetry.** Every automatic policy obeys the store's symmetry invariant.
  It is a pure function of the unordered pair plus the base: inputs are
  ordered by sha before merging, and nothing prefers "local". Independent
  resolvers therefore produce identical bytes, and the version vectors
  collapse.
- **Migration.** `mm migrate-config` rewrites `exclude_patterns` into
  `local` rules and appends new defaults, keeping user entries.

## Three-way merge

- **Bases.** A `base` table in `state.db` (`sha → bytes, last_used`) keeps
  the ledger version's bytes for paths under `merge`: text only, up to
  1 MiB. It is written at apply and at push, and pruned when no ledger row
  references it.
- **Merging.** `git merge-file -p` runs over base, side A and side B, with
  the sides in sha order. That is git's own tested line merge, and mm
  already requires git for events. Without git, the fallback applies.
- **Clean merges.** A clean result is authored with `max(vv)` plus this
  device, so it propagates to peers as a fast-forward.

## The resolver (`ask` and overlaps under `ask` fallback)

- **The conflict store.** Held versions live in the `conflicts` table:
  `(source, path, peer) → sha, vv, mtime, bytes, received_at`. Nothing is
  written next to user files, so no agent ever reads a `.sync-conflict-*`
  file as content. Store snapshots carry held versions (marked `held`), so a
  joiner sees them too.
- **Listing.** `mm conflicts` lists the store; `mm status` counts it.
- **`mm resolve`** shows base, local and remote. Its choices:
  - `(m)erge` when the merge is clean;
  - `(e)dit` in `$EDITOR`, with diff3 markers;
  - `(l)ocal` or `(r)emote`;
  - `(k)eep both`, which saves the remote copy under a new name that syncs
    if its source's rules cover it;
  - `(s)kip`, the default.
- **Decisions propagate.** Every decision authors a dominating version, so
  it propagates once, with no mtime bump.
- **`mm conflicts --suggest`** reads `history` and lists paths with repeated
  concurrent conflicts over the last 30 days. Each comes with the rule line to
  add. It is a frequency report over data mm records from 2.0 on; the
  imported 1.x rows are too few to seed it. The user chooses.

## Tracks

| Track | Delivers | Main files | Release | Size |
|---|---|---|---|---|
| **C0** Conflict corpus (no code) | Seed C2's defaults from the appendix: the 2026-09-01 forensic pass over 88 conflicts. A fresh census cannot run: `pull-history.jsonl` rotates at 1 MB, and on 2026-10-11 this Mac's two files held 20 seconds of one push, all `uploaded` rows. | this doc | done | S |
| **C2** Policies and three-way merge | `rules` config, the `migrate-config` rewrite, `union`/`merge`/`newest`/`ask`, `base` table, `git merge-file`, fallback | `config.py`, `merge.py`, new `policy.py`, `statedb.py`, `store/` apply | 2.1 | L |
| **C3** Resolver | `conflicts` table, `mm conflicts` and `mm resolve` rewrite, `--suggest`, status counts | `resolveflow.py`, `conflictdiff.py`, `cli.py`, README | 2.2 | L |

Every `verify:` uses `./bin/check <scope>`. C2 extends the store simulator
(`tests/sim/`) with a policy for each rule and re-asserts convergence and
termination.

## Decisions (recommendations marked)

1. **Clean three-way merges apply without a prompt.** *Recommended: yes*, as
   git does; `mm log` records each one.
2. **`conflict_fallback` default.** *Recommended: `newest`*, lossless via
   `displaced`.
3. **`rules` replaces `exclude_patterns`** outright, rather than living
   alongside it. *Recommended: yes*; `migrate-config` converts.

## AGENTS.md change at C2

Keep the collector ban and add: "Declared deterministic per-path policies are
not the cancelled resolver (maintainer decision, 2026-10-09). Every automatic policy must be
symmetric."

## Appendix: the conflict corpus

The only corpus is the 2026-09-01 forensic pass over all 88 `conflicted`
rows then in `pull-history.jsonl{,.1}` (2026-05-01 to 2026-09-01); the
[sync invariants](../invariants/sync.md#generated-files-are-not-sync-data-load-bearing-v01251)
record the rule it produced. It is not re-derivable: the history has since
rotated away, and `mm gc` reaps old sidecars. Conflict rate was 88 of 2,153
applied files.

| n | bucket | policy it seeds |
|---|---|---|
| 29 | derived caches (`decisions.active.json` 18, `brain-cache/_meta.json` 7, `analytics/.last-sync-*` 4) | `local` (already excluded) |
| 16 | host-regenerated tree (`~/.codex/skills/.system/**`) | `local` (already excluded) |
| 2 | per-host rendered skills | `local` (already excluded) |
| 9 | per-machine session state (`pair-review/session.yaml`, `full-review/session.yaml`) | `local` |
| 32 | remaining sync data, including cross-machine resume prose | classify by content; prose uses `merge`, fallback `newest` |

The first three buckets total 47 generator conflicts (53%); session state
takes it to 56 (64%). Resume prose such as `deploy.md`, `report.md` and
`parked-bugs.md` stays in scope; the corpus does not justify a directory-wide
`local` rule for `pair-review/` or `full-review/`.

Two cautions carry over. `union` is wrong for bounded snapshots such as
`decisions.active.json`, which omits superseded records on purpose; only
append-only logs union. And the nine surviving `conflict-decisions.jsonl`
rows all chose `remote` because the 1.x mtime gate only fires when remote is
newer, so they say nothing about preference.
