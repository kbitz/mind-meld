# Roadmap

State-organized execution plan: **In Progress** / **Current Plan** / **Future** / **Shipped**. Only shipped work has stable IDs; upcoming Groups/Tracks are volatile and re-thought on each /roadmap run. A Group is a wave of PRs that lands together — Tracks within a Group must be set-disjoint on `_touches:_` footprints.

Originating sources for the upcoming plan: 2026-08-14 fleet model-mix design + leftover unshipped polish from the May/June plan (most deferred to Future). Track 12A closed as shipped (v0.12.7).

---

## In Progress

(none)

---

## Current Plan

The fleet model-mix card (tokens + pull-request counts per host family) is the committed upcoming work. Groups 13, 14, and 15 are file-disjoint and start in parallel. Group 15 is the old tail/backfill consolidation, kept here so the host walk is grafted once.

### Phase 2: Fleet model mix

**End-state:** The retro-fleet card shows Claude / Codex / Grok by token volume and by pull-request count, fleet-wide, with mixed-fleet honesty.
**Groups:** 13, 14, 15, 16, 17

Card rows are host families (not SKUs). OpenCode classifies by model id, not as its own row. Claude tokens stay on the existing `sessions-snapshot`. Codex / Grok Build / OpenCode land in a new `host-usage-snapshot` event. Pull-request identity is unique `#N` from git-snapshot subjects (this fleet's `/ship` squash convention). Host attribution is same-day session ∩ repo, with `unknown` / `mixed` when the signal is weak.

#### Group 13: Card + pull-request totals from existing data

_Depends on: none_

User-visible first slice. No new walkers, no wire change. Claude tokens already on the wire roll up by family; every extracted `#N` starts as `unknown`.

##### Track 13A: ASCII card MODELS block + subject pull-request parser
_4 tasks . ~180 LOC . medium risk . aggregator + skill + tests_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, tests/test_retro_fleet_aggregator.py_

- **`extract_pr_numbers(subject)`** -- parse `\(#(\d+)\)`, bare `#(\d+)`, and `Merge pull request #(\d+)`. Dedup by number across remotes in-window. Pin this repo's squash-subject shape (`feat: … (#105)`). _aggregator.py, ~25 lines + tests._ (S)
- **`host_family(model)` in aggregator** -- `claude-*` → claude; `gpt-*` / `o1` / `o3` / `o4-*` / `*codex*` → codex; `grok-*` → grok; else other. OpenCode-hosted `claude-*` counts as claude. Track 17A will switch this import to `host_usage` once that module exists. _aggregator.py, ~20 lines + table tests._ (S)
- **Card + stats line** -- commits, pull-request total, repos, machines. MODELS section rolls Claude `tokens_by_model` by family; hide a host row at 0 tokens and 0 pull requests. `CARD_WIDTH` 64 pin stays green. Rename markdown `## Claude Code activity` → `## Model mix`. _aggregator.py + SKILL.md, ~80 lines._ (M)
- **GitAggregate fields** -- `prs: int`, `prs_by_host: dict[str, int]` (all `unknown` until Track 17A). Snapshot `metrics` grows `prs` additively. _aggregator.py, ~20 lines._ (XS)

#### Group 14: Host session walkers + isolated cache

_Depends on: none_

Parallel with Group 13. New module, new cache file. Must not touch `session-tokens.json` (`is_cache_cold` is global and load-bearing).

##### Track 14A: Codex / Grok / OpenCode parsers behind `host_usage.py`
_5 tasks . ~350 LOC (50% headroom for review-induced expansion) . high risk . new module + fixtures_
_touches: src/mind_meld/host_usage.py, tests/test_host_usage.py, tests/fixtures/host_sessions/_

- **`host_family` + DayBucket mapping** -- same classifier as 13A, owned here as the long-run source of truth. Map each host's usage fields onto `TOKEN_FIELDS` (`input` / `cache_create` / `cache_read` / `output`). Reasoning tokens fold into `output`, documented. _host_usage.py, ~40 lines._ (S)
- **Codex walker** -- `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. Last `event_msg.payload.type == "token_count"` `total_token_usage` per file is the session total (cumulative stream — summing every event is the bug we must not ship). Model from `turn_context.payload.model`. Incremental by size/mtime. _host_usage.py + redacted fixture, ~80 lines._ (M)
- **Grok Build walker** -- `~/.grok/sessions/<cwd>/<id>/updates.jsonl` `sessionUpdate: "turn_completed"` only. Ignore `_meta.totalTokens` (context-window size, not billed usage). Skip in-progress sessions with no completed turns. Model from `summary.current_model_id` / `usage.modelUsage`. _host_usage.py + fixture, ~70 lines._ (M)
- **OpenCode walker** -- `~/.local/share/opencode/opencode.db` read-only + busy timeout. Missing or locked DB skips that host, does not raise. Classify by `modelID`, not by app name. _host_usage.py, ~60 lines._ (S)
- **Isolated cache** -- `~/.config/mind-meld/host-tokens.json` via `lockedjson`. Deadline gate at start of each session file / SQL query. Cold+autopush skip is a CLI policy (Track 16A), not this module. _host_usage.py + tests, ~50 lines._ (S)

#### Group 15: Events-tail / backfill consolidation

_Depends on: none_

Carry-over of the load-bearing /full-review item (old Track 13A). Lands before the host-usage wire so the new walk is grafted once, not onto both copies.

##### Track 15A: `_capture_events_snapshot` + tail integration polish
_4 tasks . ~150 LOC . medium risk . src/mind_meld/cli.py_
_touches: src/mind_meld/cli.py_

- **Extract `_capture_events_snapshot(...)` helper** -- pull out the 90% shared structure between `_run_events_tail` and `_run_events_backfill` (gate, deadline math, claude_paths walk, agg_projects, s_rows). Both call sites assemble own write-list. OR fold backfill behavior into `mode={"tail","backfill"}` on a single function. _cli.py, ~80 lines reducible._ (M)
- **Lift token-cache `files_dict` resolution above the duplicated walk loop** -- the two branches under `if do_token_walk` differ only by one arg. Use `nullcontext(None)` to drop ~10 duplicated lines. _cli.py, ~10 lines._ (XS)
- **Substantive-change gate timing** -- gate sees pre-tail manifest; on UTC midnight rollover with zero source changes, no daily mm-push row lands. Verify whether monitoring/retro depends on a daily heartbeat row; either lift gate when cursor >24h stale OR document that no-op pushes don't advance the cursor. _cli.py, investigative._ (S)
- **Make `_run_events_tail` return a bool; gate source re-walk on it** -- skip the post-tail re-walk when tail didn't write (or threw). Confirm cost is below noise via wall-clock measurement before adding the signal. _cli.py, ~10 lines._ (XS)

#### Group 16: `host-usage-snapshot` on the wire

_Depends on: Group 14, Group 15_

New event type, additive on `v=2`, no `EVENTS_SCHEMA_VERSION` bump. D4: skip emits nothing (absence); walked-and-empty emits the row with `token_sources` present.

##### Track 16A: Emit host-usage-snapshot from tail and backfill
_5 tasks . ~220 LOC . medium risk . events + cli + invariants_
_touches: src/mind_meld/events.py, src/mind_meld/cli.py, docs/invariants/events-retro.md, tests/test_events.py, tests/test_init_events_backfill.py_

- **TypedDict + writer** -- `HostUsageSnapshot` (`token_sources`, `hosts[family].tokens_by_day`, `hosts[family].active_days`). `active_days` values are canonical remotes, never raw home paths. _events.py, ~40 lines._ (S)
- **Tail / backfill policy** -- after Claude `walk_done` snapshot (do not move it — v0.12.9), reset a fresh host deadline. Mirror `_decide_token_walk_policy` against `host-tokens.json`. Cold+autopush → no row. `dry_run` → no-op. Forensic `try/except`. Init backfill writes the snapshot (still no mm-push row). _cli.py, ~80 lines._ (M)
- **`active_days` at emit** -- cwd → canonical remote via existing git helpers; drop cwd-only sessions from attribution, keep their tokens. _events.py or host_usage.py call from cli, ~30 lines._ (S)
- **D4 pins** -- skip does not emit; warm-then-cold does not write an empty row that would latest-wins-wipe a prior snapshot. Aggregator fallback to last present snapshot per device is Track 17A, but the emit side must not create the wipe. _tests/test_events.py, ~40 lines._ (S)
- **Invariant doc** -- host-usage-snapshot, cache isolation, Codex cumulative, Grok `turn_completed`, budget reset, D4 skip. Pointer row in CLAUDE.md is a `/ship` docs follow-through, not this Track. _docs/invariants/events-retro.md, ~30 lines._ (XS)

#### Group 17: Aggregator merge + pull-request attribution

_Depends on: Group 13, Group 16_

Lights up Codex / Grok card rows after one fleet push. Claude cost line stays isolated so unpriced GPT/Grok volume does not flip it to `>=`.

##### Track 17A: Host token merge, attribution heuristic, mixed-fleet breadcrumb
_5 tasks . ~200 LOC . medium risk . aggregator + skill + tests_
_touches: src/mind_meld/skills/retro_fleet/aggregator.py, src/mind_meld/skills/retro_fleet/SKILL.md, docs/invariants/events-retro.md, tests/test_retro_fleet_aggregator.py_

- **Latest host snapshot per device** -- fall back to the most recent present `host-usage-snapshot` when the newest push omitted the row. Pin warm-then-cold does not show empty hosts. _aggregator.py, ~40 lines._ (S)
- **Sibling token map** -- `host_tokens_by_model` so `estimate_cost` still runs only on Claude. Roll up both maps via `host_family` imported from `host_usage`. _aggregator.py, ~30 lines._ (S)
- **Attribution** -- for each unique `#N`, `pr_day` = max commit date, `pr_repo` = canonical remote. Hosts whose `active_days[pr_day]` contains that remote (Claude: project-day in `tokens_by_day` mapped to the same remote) are candidates. 0 → `unknown`, 1 → that family, 2+ → `mixed`. Notes line names the heuristic. _aggregator.py, ~50 lines._ (M)
- **`pre_host_peers`** -- key-absence of any host-usage-snapshot for a v=2 device. Notes: `Host mix incomplete: N peer(s) on pre-host-usage mm OR with cold host-token cache`. Empty `hosts` with `token_sources` present is not flagged. _aggregator.py + SKILL.md, ~25 lines._ (S)
- **Card fill-in + snapshot metrics** -- Codex/Grok/unknown/mixed rows; persist per-host token totals additively on the v1 snapshot. Width pins. _aggregator.py + tests, ~40 lines._ (S)

### Execution Map

Adjacency list:
```
- Group 13 ← {}
- Group 14 ← {}
- Group 15 ← {}
- Group 16 ← {14, 15}
- Group 17 ← {13, 16}
```

Track detail per group:
```
Group 13: Card + pull-request totals
  +-- Track 13A ........... ~M . 4 tasks

Group 14: Host session walkers
  +-- Track 14A ........... ~L . 5 tasks

Group 15: Events-tail / backfill consolidation
  +-- Track 15A ........... ~M . 4 tasks

Group 16: host-usage-snapshot wire
  +-- Track 16A ........... ~M . 5 tasks

Group 17: Aggregator merge + attribution
  +-- Track 17A ........... ~M . 5 tasks
```

**Total: 1 phase . 5 groups . 5 tracks remaining.**

---

## Future

- **cli.py micro-cleanups (old 14A/14B)** — `_empty_outcomes` reuse, dead local re-imports, `_resolve_mm_events_dir`, skill-link status enum, marker filename convention, dead `upgrade.py` `fsutil` import. _Source: /full-review 2026-05-10._
- **safe_str hardening at three stderr sites (old 15A)** — `walk_git_projects`, `_ensure_device_registered`, `_bootstrap_mm_events_path`. _Source: /full-review._
- **Cold-cache budget leftovers (old 16A remainder)** — `allow_refresh=False` on autopush; `_FULL_GATHER_BUDGET_S` on identity gather; per-jsonl deadline in the token merge loop. The misleading budget-notice half shipped in v0.12.9. _Source: /full-review + v0.12.9._
- **identity.py micro-DRY + token-cache test pins (old 17A/B/C)** — unify `_persist`; load config once in `_do_full_gather`; `gc_cache_entries` `max_age_s=0`; positive cache-isolation test. _Source: Track 11A eng-review._
- **v0.11.17 doc-drift cleanup (old 18A)** — events-retro dead-name list; aggregator docstring; `walk_session_metadata(since)` unused param. _Source: /full-review._
- **Incremental-resume accepted divergences** — tool_use id not seeded across segments; final line without trailing newline never counted. Evidence-triggered only (census). _Source: [review] inbox._
- **Rip out CONFLICT-TELEMETRY collector** — after Phase 2 bands validate (≥25 real decisions or 60 days). _Source: [plan-eng-review] inbox._
- **Future-clamped peer mtime can mislead `(n)ewer`** — advisory watch. _Source: [plan-eng-review] inbox._
- **`_promote_target_will_sync` ignores `exclude_patterns`** — rare exclude-glob miss. _Source: [review] PR #97._
- **Phase 2 similarity classifier + silent merge** — blocked on collector data. _Source: [plan-eng-review] inbox._
- **Peers we never resolved against can be mtime-skipped by the drain** — watch now that 12A shipped. _Source: [plan-eng-review] inbox._
- **Abort transactionality** — pre-existing torn-state. _Source: [review] inbox._
- **Price cache writes per-TTL (5m vs 1h)** — wire-format change; competes with host-usage on `token_usage.py`. _Source: [plan-eng-review] inbox._
- **`test_gc_events.py` touches the real mind-meld.lock** — flake against autopush. _Source: [review] inbox._
- **Model-id variant suffixes alias onto base model** — no real variant ids in census. _Source: [review] inbox._
- **No sanity ceiling on rendered cost figure** — presentation-layer. _Source: [review] inbox._
- **Parallel blob fetch in `_download_and_apply`** — wrap per-file `backend.get(bkey)` in `concurrent.futures.ThreadPoolExecutor(max_workers=8)` + `as_completed` (mirror `events.py:walk_git_projects`'s shape). Submit-all-upfront pattern (D2): all N futures live in the executor at once with peak memory ≈ N × avg_enc_blob_size (~70MB on the measured 1449-blob workload, ~500MB at 10k blobs). Measured 7.3× speedup (509ms → 143ms per blob) on a fresh-Mac iCloud-cold pull. _Source: /plan-eng-review 2026-05-06. Deferred because Track 9A's auto-pin prevents the slow-pull case at source. Parallelization only helps users on a non-iCloud storage path or those whose auto-pin was revoked. Revisit on user-reported sustained slow pull (>30s) AFTER auto-pin OR fleet hits 10k+ blobs._

- **Selective sync (`sync.include` / `sync.exclude`)** — per-project filtering so users with dozens of Claude projects can sync just the 2-3 they actively use across machines. _Source: triage. Deferred because no user demand signal yet; revisit on first support case from someone with dozens of projects._

- **Mtime hash cache** — push-side perf: skip re-hashing files whose mtime hasn't changed since the last push. Per-device local cache at `~/.config/mind_meld/local-manifest.json` keyed by (mtime, size, sha). _Source: triage. Deferred because crypto v2 already solved the motivating push-latency problem (process-scoped master key + HKDF). Revisit only if push latency becomes user-visible again._

- **Three-way merge base (stored last-synced hash)** — pull-side correctness upgrade: per-source, per-file last-synced hash. Distinguishes "remote changed, I didn't" from "we both changed" — fast-forward when only one side changed; conflict-copy only when both diverged from base. Hard prereq for upgrading Track 5B's `git merge-file` to a 3-way merge. _Source: triage. Deferred because no divergence-misclassification reports today. Revisit if users report "it conflict-copied a file I didn't even touch."_

- **`mm rekey` passphrase rotation** — Format v2 makes `master_key` the rotation boundary but v2 blobs don't carry a `key_scheme` byte. Rotation requires format v3: either re-wrap `master_key` under the new passphrase, or re-encrypt every blob under a freshly-derived `master_key`. _Source: SPEC. Deferred as post-1.0 P3 — requires format v3 + migration dance; no users blocked pre-1.0._

- **Blob-directory as secondary peer-discovery in corrupt-manifest recovery** — in `_collect_peer_tombstones`, when a peer's `devices/<id>.json` is corrupt or missing but `data/<id>/` has blobs and `manifests/<id>/*.enc` decrypts, promote the blob-dir-derived `device_id` to the peer list. Widens the trust surface — blob-presence becomes load-bearing evidence of a peer's existence. _Source: adversarial. Deferred until first real support case appears where corrupt `devices.json` masks a recoverable manifest._

- **PyPI publish workflow** — `.github/workflows/release.yml` that builds + publishes to PyPI on git tag push. Uses `hatchling` build backend (already configured). Currently users install via `pip install -e .` from a local clone; PyPI distribution would let someone `pip install mind-meld` cleanly. _Source: triage. Deferred until "how do I install this" becomes friction. No user demand signal today._

- **Cross-device source rename drift partitions sync** — Track 2A's type-keyed sync-log fix addressed *same-device* renames. Cross-device, manifests are still keyed by `src_name`, so if device A renames "claude" → "work-claude" but B keeps "claude", B's pull skips A's manifest via the unknown-source warning path. Cross-device source identity needs to key off `(type, signature)` or similar, not raw name. _Source: codex adversarial 2026-04-24. Deferred because no fleet-rename incident yet; documenting as a known limitation for v1.0 is enough._

- **`mm upgrade-info` (or `mm version --check`) explicit-check command** — Today the auto-upgrade nudge fires once per 24h on autopull/autopush/interactive paths and `mm status` surfaces cached state. There's no "check NOW" command. Cleanest shape likely `mm status --refresh` (a flag on the existing command) rather than a new top-level command. _Source: /plan-ceo-review. Deferred — original write-up says "Watch for real demand before designing — ship the baseline, see if `mm status` is enough."_

- **Approach B: subprocess pipx upgrade execution** — v0.9.5 ships nudge-only ("Approach A"). Approach B would add `mm upgrade` running pipx as subprocess so the user doesn't type the install command themselves. Real complexity: managed-pipx detection (Homebrew / asdf), rollback ambiguity if install fails partway, TTY detection for interactive prompts. _Source: /plan-ceo-review. Deferred until Approach A has been in production ≥1 release cycle and printed-command UX feels insufficient._

- **`MM_NO_VERSION_CHECK=1` env var as alternate CI override** — Redundant with the `--no-check-version` flag. _Source: /plan-ceo-review. Deferred — add only if env-var ergonomics surface as real demand (e.g., a CI hook that wants to set the override once for all mm invocations)._

- **Pagination beyond 100 tags for `/repos/kbitz/mind-meld/tags`** — `upgrade.py` fetches with `?per_page=100` (max). At current release velocity (30 tags / 6 months) this gives ~3 years of headroom. Past 100 tags, latest detection may miss the highest semver if GitHub's tag sort places older tags on page 1. _Source: /plan-eng-review. Deferred — ~3 years of headroom before the cap matters._

- **`tests/conftest.py::_isolate_devices_write_lock` couples every test to `mind_meld.devices` import** — autouse fixture imports `mind_meld.devices` so it can monkeypatch `DEVICES_WRITE_LOCK`. Couples otherwise-independent tests (test_wheel.py, test_version.py) to the devices module's import chain. Forward-defense fix: scope the fixture narrower (consumed explicitly by tests touching devices), OR move `DEVICES_WRITE_LOCK` to a config-style constants module. _Source: ship pre-landing review 2026-04-27. Deferred as forward-defense — no real-world failure has surfaced from the coupling._

- **`[retro].deny_emails` subtractive override** — fleet-wide author-email trust set (v0.11.17 `identity.py`) is additive only via union of every peer's `local_emails`. To remove an email (stolen credential, wrong-account commit, deprecated alias) the user must wait for the 90-day events retention to age it out. Add `[retro].deny_emails: list[str]` config knob; aggregator subtracts the denylist after the additive union. Symmetric with the existing `[retro].author_emails` additive knob. _Source: /plan-eng-review (defer-tagged). Deferred — symmetric design ready when demand surfaces (credential leak, account hygiene)._

- **Skills-walk-complete signal as explicit schema field** — add `skills_walk_complete: bool` on the v=2 sessions-snapshot project metadata (additive, total=False, no schema bump); aggregator picks the latest snapshot per `(device, source_root, claude_dir)` where `skills_walk_complete=True` for skills aggregation, falls back to flagging `skills_incomplete_peers` when no completed walk exists in window. Three-state discriminator (pre-v0.11.27 / complete / skipped) replaces v0.12.4's cosmetic-only breadcrumb. _Source: /plan-eng-review 2026-05-10 (deferred from Track 11B Option B). Re-evaluate when pre-v0.11.27 peers age out and cold-cache-push disambiguation becomes operationally valuable. Do NOT reintroduce the rejected 3-LOC `events.py:_scan_one_project` fix (dropping `if token_cache_files is not None:`) — it causes latest-snapshot-wins data erasure on warm-then-cold push ordering. See CHANGELOG v0.12.4 and `docs/invariants/events-retro.md`._

## Shipped

### Phase 1: v0.x → v1.0 cleanup sweep ✓ Shipped (v0.11.24)

**End-state:** Decomposition + DRY, init flow, test hygiene, release infra, conflict UX, mm-events foundation, retro-fleet skill, post-1.0 polish — all shipped through v0.11.24.
**Groups:** 1, 2, 3, 4, 5, 6, 7, 8, 9, 10

#### Group 1: Decomposition + DRY ✓ Shipped (v0.8.4–v0.8.6)

The proposed `constants.py` extraction preflight was dropped on 2026-04-24 after a `/plan-eng-review` cohesion check: only `FORMAT_VERSION` and `CONFLICT_INFIX` are genuinely cross-module of the four candidates, and even those pair tightly with siblings the original task missed.

- Track 1A — _shipped (v0.8.4): decompose `_pull_core` + `_apply_incoming_file`. 2 tasks shipped._
- Track 1B — _shipped (v0.8.5): walker + manifest + merge DRY. 3 tasks + 1 contract-change cleanup shipped._
- Track 1C — _shipped (v0.8.6): post-1A cli.py follow-ups — diff-call-site DRY, GC blob-shape validation, autopull degraded breadcrumb. 3 tasks shipped._

#### Group 2: Init flow + sync_log generalization + config polish ✓ Shipped (v0.8.7–v0.8.8)

Multi-source assumption lag resolved — `init` no longer hardcodes `~/.claude` and `write_sync_log` is type-keyed instead of name-keyed.

- Track 2A — _shipped (v0.8.7): init decomposition + DEFAULT_SOURCES reuse + sync_log generalization. 5 tasks + refinements shipped._
- Track 2B — _shipped (v0.8.8): config polish — backfill path preservation + ConfigError prefix rename. 2 tasks shipped._

#### Group 3: Test hygiene + style polish ✓ Shipped (v0.8.10)

Pre-flight style polish (type hints, optional syntax, placeholderless f-strings, keyring except-narrowing) + Track 3A test improvements all landed together. Codex `/review` caught a P0 keyring-propagation gap during review.

- Track 3A — _shipped (v0.8.10): CliRunner migration + combined push/pull/conflict E2E + 86 lazy-import hoists. 3 tasks shipped + 3 codex-follow-through pins._

#### Group 4: Release infrastructure ✓ Shipped (v0.8.11)

Single `macos-latest` + Python 3.13 GitHub Actions workflow runs `ruff check`, `ruff format --check`, `pytest tests/`, wheel build + install + `mm --version` smoke, and asserts the real Keychain backend loads.

- Track 4A — _shipped (v0.8.11): GitHub Actions CI + ruff pin + README badge + 113-violation drift sweep. 1 task shipped._

#### Group 5: Conflict UX & first-pull polish ✓ Shipped (v0.8.15–v0.9.4)

Surfaced 2026-04-24 from a fresh-Mac first-pull session. Tracks 5A/5B/5C/5E shipped through v0.9.2 (plus a v0.9.3 hotfix patch); Track 5D shipped as v0.9.4.

- Track 5A — _shipped (v0.8.15): auto-command silent-mode + scope bugs + Group 5 preflight. 3 tasks + 1 preflight._
- Track 5B — _shipped (v0.9.0): pull / resolve / conflicts UX surfaces. 4 tasks + scope expansions. **BREAKING.** 14 new tests._
- Track 5C — _shipped (v0.9.1): exclude_patterns + log + migration UX. 38 tests + 5 IRON RULE pins. Pivoted via /plan-ceo-review from "conflict inversion + real-merge backends."_
- Track 5D — _shipped (v0.9.4): Track 5A adversarial-review follow-ups — `_find_conflict_files` dedup, `_register_and_save` order swap with cleanup, push-time self-heal. 15 new tests._
- Track 5E — _shipped (v0.9.2): conflict default inversion. **BREAKING.** Added `packaging>=21.0`. 11 tests in `TestInversion5E`._
- _v0.9.3 hotfix patch (post-Track-5C): added `config.yaml` to gstack source's default `exclude_patterns`._

#### Group 6: Release infrastructure polish ✓ Shipped (2026-04-27)

Single-track Group bundling release-discipline polish. Independent of the auto-upgrade code path.

- Track 6A — _shipped: GitHub Releases backfill — 36 release entries created via `gh release create`, one per existing tag v0.1.0..v0.10.0. 0 source LOC._

#### Group 7: mm-events foundation (event capture) ✓ Shipped (v0.10.1–v0.10.3)

Implements the foundation for the fleet-aware retro per `docs/archive/fleet-retro.md`: per-device JSONL event log written on every push.

- Group 7 preflight — _shipped (v0.10.1): security/concurrency/correctness sweep + mm-events default source. 8 items shipped._
- Track 7A — _shipped (v0.10.2): events.py module — canonicalize_remote_url, walk_git_projects budget+concurrency, walk_session_metadata Conductor-ephemeral detection, cursor + write_push_event under flock. 4 tasks shipped._
- Track 7B — _shipped (v0.10.3): _push_core wiring + gc retention. 3 tasks shipped._

**Hotfix:** `get_sources` bootstrap fires on every read-only command — _shipped v0.11.2 (2026-04-29). Module-level `_BOOTSTRAP_WARNED_PATHS` short-circuits subsequent calls in same process; pinned by regression test._

#### Group 8: retro-fleet skill (consumer) ✓ Shipped (v0.11.0)

User-facing surface of the fleet-aware retro: a Claude Code skill shipped in the mm wheel and symlinked into `~/.claude/skills/` at `mm init`.

- Track 8A — _shipped (v0.11.0): SKILL.md + aggregator + symlink installer + CI smoke test. ~1100 LOC; 1107/1107 tests pass; 13 review-applied lock-ins from /plan-eng-review._

**Hotfix** (post-ship; serial):
- _Aggregator `mm-events` custom-path notice [adversarial 2026-04-28]: print `mm: notice:` from aggregator when default events dir is empty but config has non-default `mm-events` path._
- _Aggregator memory: streams + filters per-file rather than slurping entire corpus [adversarial 2026-04-28]._
- _walk_session_metadata budget: pin benchmark + bump budget OR reintroduce mtime fast-path [adversarial 2026-04-28]._
- _Sessions dedup key: include source root path to avoid encoded-name collisions across multiple Claude source roots [adversarial 2026-04-28]._
- _Mid-upgrade peer "pre-v0.11.0" breadcrumb persists after upgrade [adversarial 2026-04-28]: window naturally moves past v=1 snapshots within 7-30 days._

#### Group 9: Pull performance + fresh-Mac onboarding ✓ Shipped (v0.11.23)

Surfaced 2026-04-27 from a pull-perf dogfood session on kb's 349C-kb-ms. Scope reduced via /plan-eng-review (2026-05-06) from a 2-task plan paired with 150-line parallel-fetch optimization to a 5-line auto-pin nudge — once storage is pinned, `mm pull` reads resident blobs and is already fast (<5s on 1449-blob workload).

- Track 9A — _shipped (v0.11.23): auto-pin iCloud storage on `mm init` via `brctl download`. 1 task shipped._

#### Group 10: Token-usage post-ship cleanup ✓ Shipped (v0.11.24)

Four DRY + perf items deferred from /ship pre-landing reviews of the v0.11.14+ token-usage work. All scoped to internal hygiene — no public-API change, no user-visible behavior change.

- Track 10A — _shipped (v0.11.24): token-usage DRY + perf polish. Consolidated 4 bucket-merge sites behind `merge_usage_bucket` / `merge_by_model` + `TOKEN_FIELDS` constant + `zero_day_bucket` / `zero_model_bucket` factories. 4 tasks shipped._

### Group 11: Token-cache + cold-cache correctness fixes ✓ Shipped (v0.12.4–v0.12.5)

The two `necessary` correctness fixes from /full-review 2026-05-10. Loose Group — post-1.0, outside the v0.x → v1.0 sweep above.

- Track 11A — _shipped (v0.12.5): token-cache invariant ownership consolidation — autouse `_isolate_token_cache` fixture + `gc_cache_entries` routed through `lock_and_get_files`. Cross-model HIGH (unknown-top-level-key-stripping regression) caught and fixed during /review. 5 new pinning tests._
- Track 11B — _shipped (v0.12.4): cosmetic-only "Skills incomplete" breadcrumb admits cold-cache push as a second cause. Original Option B (3-LOC `events.py` fix) rejected during /plan-eng-review — would cause latest-snapshot-wins data erasure on warm-then-cold push ordering._

### Group 12: inline keep-canonical mtime bump ✓ Shipped (v0.12.7)

Deferred the inline `keep-canonical` mtime bump to end-of-pull-batch so `mm pull --conflict-mode prompt` choosing (l)ocal propagates across the fleet without mid-walk later-peer skip.

- Track 12A — _shipped (v0.12.7): `pending_inline_bumps` drained after all peer walks. 13 tests in `TestResolveLocalMtimeBump`._
