# Shipped

<!-- Frozen shipped history. Append-only. IDs never recycle. Maintained by /roadmap. -->

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
- Track 5C — _shipped (v0.9.1): exclude_patterns + log + migration UX. 38 tests + 5 IRON RULE pins. Pivoted via /plan-ceo-review from conflict inversion + real-merge backends._
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
- _Mid-upgrade peer pre-v0.11.0 breadcrumb persists after upgrade [adversarial 2026-04-28]: window naturally moves past v=1 snapshots within 7-30 days._

#### Group 9: Pull performance + fresh-Mac onboarding ✓ Shipped (v0.11.23)

Surfaced 2026-04-27 from a pull-perf dogfood session on kb's 349C-kb-ms. Scope reduced via /plan-eng-review (2026-05-06) from a 2-task plan paired with 150-line parallel-fetch optimization to a 5-line auto-pin nudge — once storage is pinned, `mm pull` reads resident blobs and is already fast (<5s on 1449-blob workload).

- Track 9A — _shipped (v0.11.23): auto-pin iCloud storage on `mm init` via `brctl download`. 1 task shipped._

#### Group 10: Token-usage post-ship cleanup ✓ Shipped (v0.11.24)

Four DRY + perf items deferred from /ship pre-landing reviews of the v0.11.14+ token-usage work. All scoped to internal hygiene — no public-API change, no user-visible behavior change.

- Track 10A — _shipped (v0.11.24): token-usage DRY + perf polish. Consolidated 4 bucket-merge sites behind `merge_usage_bucket` / `merge_by_model` + `TOKEN_FIELDS` constant + `zero_day_bucket` / `zero_model_bucket` factories. 4 tasks shipped._

### Group 11: Token-cache + cold-cache correctness fixes ✓ Shipped (v0.12.4–v0.12.5)

The two `necessary` correctness fixes from /full-review 2026-05-10. Loose Group — post-1.0, outside the v0.x → v1.0 sweep above.

- Track 11A — _shipped (v0.12.5): token-cache invariant ownership consolidation — autouse `_isolate_token_cache` fixture + `gc_cache_entries` routed through `lock_and_get_files`. Cross-model HIGH unknown-top-level-key-stripping regression caught and fixed during /review. 5 new pinning tests._
- Track 11B — _shipped (v0.12.4): cosmetic-only Skills incomplete breadcrumb admits cold-cache push as a second cause. Original Option B 3-LOC events.py fix rejected during /plan-eng-review — would cause latest-snapshot-wins data erasure on warm-then-cold push ordering._

### Group 12: inline keep-canonical mtime bump ✓ Shipped (v0.12.7)

Deferred the inline `keep-canonical` mtime bump to end-of-pull-batch so `mm pull --conflict-mode prompt` choosing local propagates across the fleet without mid-walk later-peer skip.

- Track 12A — _shipped (v0.12.7): `pending_inline_bumps` drained after all peer walks. 13 tests in `TestResolveLocalMtimeBump`._

### Group 14: Symlink policy on both push and apply paths ✓ Shipped (v0.12.17)

Child symlinks are now local routing rather than sync content: generic walkers omit them, pull preserves live and dangling local links, and source roots may still be symlinked. Prior-manifest filtering prevents this policy change from generating deletion tombstones; generated Codex/OpenCode skill trees are excluded without excluding hand-authored skills.

- Track 14A — _shipped (v0.12.17): tombstone-safe child-symlink suppression, pull-time link preservation, generated-skill exclusions, invariant documentation, and regression coverage._

### Group 13: Hotfix: events-tail malformed-byte resilience ✓ Shipped (v0.12.16)

- Track 13A — _shipped (v0.12.16): binary-safe session reads, degradation breadcrumb, and regression coverage._

### Group 15: Post-ship cleanup sweep ✓ Shipped (v0.12.18–v0.12.20)

- Track 15A — _shipped (v0.12.18): remove obsolete token-usage exports and migrate their test coverage._
- Track 15B — _shipped (v0.12.20): remove frozen state-path constants and the dead upgrade import._
- Track 15C — _shipped (v0.12.19): make retro-aggregator imports explicit and remove the obsolete importer._

### Group 16: cli.py decomposition ✓ Shipped (v0.12.21)

- Track 16A — _shipped (v0.12.21): remove the unused collector, extract six cohesive modules, tighten isolation and routing gates, and update release documentation._

### Group 17: Post-extraction and host-reader foundations ✓ Shipped (v0.12.22–v0.12.25)

- Track 17A — _shipped (v0.12.24): descriptor-driven Claude/Codex/OpenCode skill installation with truthful partial results._
- Track 17B — _shipped (v0.12.25): one private event-capture path shared by push and init while retaining their separate policies, writes, and budgets._
- Track 17C — _shipped (v0.12.24): canonical host-family classification, isolated incremental Codex usage cache, and bounded local reader._
- Track 17D — _shipped (v0.12.23): retention reapers plan before mutation, including byte/metadata-preserving dry-run coverage._
- Track 17E — _shipped (v0.12.22): distinct repository-qualified GitHub PR identity in retro aggregation._

### Group 19: Host snapshot writer and resolver hygiene ✓ Shipped (v0.12.32–v0.12.33)

- Track 19A — _shipped (v0.12.33): additive, all-or-nothing host-usage snapshots from the event tail and init backfill._
- Track 19B — _shipped (v0.12.32): resolver continuation and skip-default cleanup following the rendering seam._

### Group 20: Host snapshot wire contract ✓ Shipped (unreleased)

- Track 20A — _shipped: documented and pinned the complete, coverage-aware snapshot contract that distinguishes omission from an explicit empty observation._

### Group 18: Finish the non-Claude host-usage foundation ✓ Shipped (v0.12.26–v0.12.34)

The host reader, model-family card baseline, root budgets, CLI safety work,
and the Grok v1 terminal-usage reader have landed.

- Track 18A — _shipped (v0.12.26): post-decomposition CLI safety sweep._
- Track 18B — _shipped (v0.12.31): centralize conflict diff rendering and choice normalization._
- Track 18C — _shipped (v0.12.28): budget root discovery and no-op heartbeat behavior._
- Track 18E — _shipped (v0.12.29): render model-family rows from existing data._
- Track 18D — _shipped (v0.12.34): read Grok Build's v1 terminal usage ledger._

### Group 21: Opt in and publish Grok usage snapshots ✓ Shipped (v0.12.34)

- Track 21A — _shipped (v0.12.34): gate and publish trusted Grok usage. At 21A, `mm enable-source grok` was usage-only; Track 22B later added its scoped sync source._

### Group 22: Host snapshot merge ∥ Grok customization source ✓ Shipped (v0.12.35–v0.12.36)

- Track 22A — _shipped (v0.12.36): retain each device's latest complete host-usage snapshot as last-known-good inventory. Host-family day maps stay whole, never sliced or summed into fleet window spend. Hidden `mm retro-fleet --dump-host-usage` for forensics._
- Track 22B — _shipped (v0.12.35): `mm enable-source grok` adds the one scoped `type = "grok"` source and retains the 21A host-usage consent bit. The walker syncs only `skills/`, `commands/`, and `rules/`; sessions, credentials, config, bundled files, and child links remain local._

### Group 23: MODELS card coverage ✓ Shipped (v0.12.37)

- Track 23A — _shipped (v0.12.37): the `AGENT LOGS` block reports per-model-family activity rhythm and never magnitude, `MODELS` states its own provenance in the header, and an absent block always names its cause._

_Track 23B ("Install retro-fleet into Grok skills") was dissolved on 2026-08-20
after failing its `/autoplan` premise gate 6/6 across two models: the installer's
mkdir would have manufactured the Grok consent signal, the 4-file scope estimate
ran against an 11-file empirical baseline, and its `blocked-by` edge on 22A was
fabricated. Its intent survives as Track 27A; the prerequisites it was missing are
Groups 24 through 26. Review:
`~/.gstack/projects/kbitz-mind-meld/kbitz-track-23b-autoplan-phase1-ceo-20260820.md`._

### Group 24: Durable skill store ∥ Deterministic retro trends ✓ Shipped (v0.12.38–v0.12.39)

- Track 24A — _shipped (v0.12.38): published the mm-owned `retro-fleet` skill store at `~/.local/share/mind-meld/agent-skills/`, migrated live links safely, and made a wedged link diagnosable in one `mm diag` step without touching an ephemeral checkout._
- Track 24B — _shipped (v0.12.39): replaced the machine-local snapshot trends with a prior equal-period comparison computed from the synced event corpus in one in-memory pass. `--no-save` became a hidden compatible no-op, safe snapshot leftovers are reaped, and coverage is derived from the earliest event-file date rather than the retention constant._
