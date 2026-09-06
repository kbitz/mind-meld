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
`~/.gstack/projects/kbitz-mind-meld/kbitz-track-23b-autoplan-phase1-ceo-20260820.md`.
Lineage ends there: Track 27A was itself killed on 2026-08-25 — see the Group 27
tombstone below. 23B's premise gate was right for a reason neither run saw at the
time, which is that Grok never needed a link at all._

### Group 24: Durable skill store ∥ Deterministic retro trends ✓ Shipped (v0.12.38–v0.12.39)

- Track 24A — _shipped (v0.12.38): published the mm-owned `retro-fleet` skill store at `~/.local/share/mind-meld/agent-skills/`, migrated live links safely, and made a wedged link diagnosable in one `mm diag` step without touching an ephemeral checkout._
- Track 24B — _shipped (v0.12.39): replaced the machine-local snapshot trends with a prior equal-period comparison computed from the synced event corpus in one in-memory pass. `--no-save` became a hidden compatible no-op, safe snapshot leftovers are reaped, and coverage is derived from the earliest event-file date rather than the retention constant._

### Group 25: Registry hygiene ∥ SKILL.md preflight ∥ Install consent ✓ Shipped (v0.12.40–v0.12.42)

Never marked at the time. The Group's own regeneration ran *inside* the v0.12.41
PR, ahead of that PR's feature commit, so 25B shipped unmarked and 25C shipped
unmarked behind it. Both were reconciled from git on 2026-08-25, three releases
late. Track 28B exists to gate that recurrence.

- Track 25A — _shipped (v0.12.40): derived the skill links from an agent registry; `AGENT_ROWS` became canonical and policy-ready, replacing seven ad-hoc per-agent enumerations at this site. 2 tasks shipped._
- Track 25B — _shipped (v0.12.41): an unskippable Step 0 preflight for `retro-fleet` (one terminal rule, a standalone `command -v mm` gate that also catches a resolvable-but-broken binary, upgrade-notice relay) plus the README troubleshooting rewrite. No version comparison, deliberately. 2 tasks shipped._
- Track 25C — _shipped (v0.12.42): gated the writes the way the reads were already gated. `[skills] maintain_links` and the optional exhaustive `[skills] agents` allowlist, `consented_agent_keys` as the one derivation, `AgentRow.consent_source`, the installer `declined` status, `mm install-skills --agent KEY`, and a one-time transition notice. Fixed unconsented writes into `~/.codex/skills/` and `~/.config/opencode/skills/`. 2 tasks shipped._

### Group 26: Grok skill discovery, resolved without a registry row ✓ Shipped (v0.12.43)

Unplanned — this was on no card before it shipped. Minted here at the first free
integer after shipped history so that the kill of Track 27A has a visible cause
rather than looking like scope that quietly evaporated.

- Track 26A — _shipped (v0.12.43): `mm diag --json` gained the `host_skill_discovery` sibling key — a 2s `grok inspect --json` probe (argv, no shell, capped stdout) with five explicit failure states, `mm diag` only. Also made `may_create` keyword-required on the writers so a forgotten kwarg is a `TypeError`, and removed two zero-caller installer helpers. Established the registry exit criterion: **mm maintains a skill link only for hosts that do not discover `~/.claude/skills`** — verified 2026-08-24 against Grok 1.0.5, which discovers that directory at the same documented priority tier as `~/.grok/skills` via its default-on Claude compatibility layer._

### Group 27 — tombstoned, never shipped

"Grok row" / Track 27A, killed 2026-08-25. Group 26 above shipped the opposite
conclusion and a written exit criterion that refuses the row, so the Track was
killed rather than discharged: the row was never added and now never will be.
Phase 2's end-state clause it served is met by other means and was restated
rather than dropped. The number is retired so that anyone re-anchoring on
"Group 27: Grok row" does not land on Group 28's uninstall work.

### Group 28: Symmetric uninstall ✓ Shipped (v0.12.44)

- Track 28A — _shipped (v0.12.44): deleting a `retro-fleet` skill link now sticks. `mm push` stops treating an absent target as damage when a success marker proves mm installed there, so `rm ~/.codex/skills/retro-fleet` is the whole procedure and no config edit is involved. Damage — `dangling-ours`, `dangling-ours-legacy`, `foreign` — still repairs exactly as before; only an ABSENT link counts as intent. `mm install-skills` (`explicit=True`) is the undo. `mm diag` splits `removed-by-user` from `absent`, closing the read-back gap. Shell completion enabled (`add_completion=True`). The one-time 0.12.42 policy-transition machinery was deleted in the same PR — 7 symbols (the TODO named 6; `_join_display_names` was found during the Track), 5 stale `__all__` entries, 3 `cli.py` call sites, 5 test sites — while its README troubleshooting entry was deliberately KEPT._

  **The promised `mm uninstall-skills` command was refused, not deferred.** `/autoplan` established that a revoke command, a `[skills] revoked` denylist, and a third policy axis were all downstream of one defect: the installer resurrected a link the user deleted. No normal CLI tool needs a denylist to make removal stick — it needs an installer that does not resurrect. `README.md`'s forward reference was retired. The `CHANGELOG.md` and `docs/PROGRESS.md` mentions sit inside the shipped 0.12.42 rows and were left verbatim — release history is append-only, so the v0.12.44 rows supersede the promise rather than rewriting the release that made it. Full design record, including the four rejected command shapes and the two outside voices that rejected all of them: `~/.gstack/projects/kbitz-mind-meld/kbitz-installer-uninstall-autoplan-plan-20260825-090506.md`.

- Track 28B — killed 2026-08-25, never shipped. Roadmap-staleness gate (a release drift budget parsing `✓ Shipped (vX.Y.Z)` markers against `CHANGELOG.md` releases, failing when drift exceeds N). Sixth occurrence of the underlying problem, and killed anyway on the user's call: with Group 28 closed and the Current Plan empty, there is no in-flight work left to drift, so the condition that generated six occurrences is removed rather than guarded. The concrete design is recorded here so a seventh occurrence — once new Groups are in flight — can pick it up instead of re-deriving it.

Phase 2 (Durable agent skill links) completes here: Groups 24, 25, 26, 28.
Group 27 stays tombstoned.

### Group 29: Repository discovery ✓ Shipped (v0.12.45)

Phase 3 (Retro fidelity) opens here. The Phase exists because the shipped card was
not merely incomplete, it was confidently wrong: measured 2026-08-25 for a 7-day
window, `mm retro-fleet 7d` reported 4 commits / 1 repo / 1 PR reference against a
ground truth on one device of 41 commits / 4 repos / 26 PR references, with
mind-meld's own ten in-window commits absent from its own retro.

- Track 29A — _shipped (v0.12.45): git-root discovery finds the repositories that are actually on the machine. `_is_git_toplevel`'s per-candidate `git rev-parse` subprocess (7.84 ms live, 7.11 ms for a path that no longer exists, against 0.0061 ms for a `.git` stat — a 1300x ratio) was replaced by a `.git` + `HEAD` / `gitdir:` sniff, keeping the `.git`-file case that Conductor worktrees need. A `~/conductor/workspaces/*/*` prober was added as a third source, because `_probe_claude` structurally cannot see a workspace with no `~/.claude/projects` entry — four such workspaces had in-window commits. The dead `_probe_gstack` prober was deleted. `ROOT_DISCOVERY_BUDGET_*` was re-tuned from the post-fix measurement and recorded in the invariant doc. Codex `turn_context.cwd` and Grok's URL-encoded session dir names were refused: they would yield more roots but put an encoded cwd on the wire._

### Group 30: Cursor integrity ✓ Shipped (v0.12.46)

- Track 30A — _shipped (v0.12.46): a push whose discovery was incomplete can no longer silently orphan that interval's commits. Of six `git-snapshot` rows in the measured window, three captured zero projects. Incomplete discovery now HOLDS the git cursor, but the mm-push row is still written so diagnosis and author attribution survive. Walk failures never hold the cursor — git-walk cost grows with cursor age (48.6 ms at 1 day, 251.3 ms at 30 days against a 250 ms autopush budget), so holding on a walk abort would wedge unattended autopush. `git_capture` was added to the mm-push row (`since`, `discovery`, `walk_budget_aborts`, `walk_errors`; absence is the version discriminator, fail-open). `mm recapture [WINDOW]` shipped as the dedicated recovery command — deliberately NOT `mm push --recapture`, which would have re-opened the v0.12.2 phantom-change gate. The degradation reaches the autopush `degraded` breadcrumb, satisfying the AGENTS.md rule that a new tail degradation must be appended to the list, not merely printed._

### Group 31: Grok reader tolerance ✓ Shipped (v0.12.47)

- Track 31A — _shipped (v0.12.47): enabling the Grok source stopped advertising a feature guaranteed to publish nothing. Two unmodeled-but-benign wire variants were fatal to the whole scan: one session dir without `updates.jsonl` (`_is_regular_non_symlink` converted `FileNotFoundError` into `io_error`, and 1 of 79 dirs zeroed everything), and four usage-less cancelled `turn_completed` records out of 193 (2% of records destroying 100% of reporting). The absent-file tolerance was narrowed to `FileNotFoundError` / `NotADirectoryError` and shared with the Codex walker, closing an `iterdir()`-then-`lstat()` TOCTOU there too. The usage-less carve-out had to be inserted BEFORE the exact key-set check — placed at the usage site it would have been dead code, which both outside voices caught independently. Reader-scoped failure isolation shipped alongside: a Grok format change drops Grok, declared in additive `degraded_sources`, and Codex still publishes. `mm diag` gained a `host_usage` block and a Grok wire-contract census landed at `tests/fixtures/host_sessions/grok/CONTRACT.md`._

  **Caveat carried forward:** `degraded_sources` reaches the wire and the autopush breadcrumb, but the retro card never reads it — zero references in `aggregator.py`. Found during Track 32A `/review` on 2026-08-28 and placed as Track 34A.

### Group 32: Codex per-turn reader ✓ Shipped (v0.12.48)

- Track 32A — _shipped (v0.12.48): Codex usage is now counted per turn and no longer counted twice. The card that reached this Group proposed summing `info.last_token_usage` and blamed the non-reconciling files on resumed sessions; `/autoplan` measured that central premise FALSE against the live 746-rollout corpus. Actual cause: duplicate `token_count` records (183 files, 414 records) where the total stays flat and `last` repeats — only 4 files are resumes, and the card proposed the estimator that causes the larger error. The bigger omission the card never named: **a rollout file is not a session.** 195 `turn_id` values span 244 of 746 files sharing 85% of their ledger before diverging, so per-file summing double-counted over half the reported total; fixing the estimator alone would have made a ~2x-wrong number precise. Shipped: a per-turn estimator differencing `total_token_usage` between consecutive readings (reconciles exactly on 480 of 480 non-forked rollouts across all four counters), cross-file turn dedup keyed by `(lineage, previous, current)` over connected components of `turn_id`, an interned cache shape with a key-absence migration gate (NOT a `CACHE_VERSION` bump — that constant is shared with the Grok and OpenCode namespaces), and the 8 documentation locations that asserted the cumulative premise, one already false for Grok as of v0.12.47._

### Group 33: Per-model host wire ✓ Shipped (v0.12.49)

- Track 33A — _shipped (v0.12.49): the host-usage snapshot carries per-model per-day buckets, derived from the existing v0.12.48 cache with no re-walk. The load-bearing constraint was that the map ship as an ADDITIVE SIBLING key rather than by widening `hosts`: `aggregator._copy_usage_bucket` validates a day bucket with an exact key-set match and a rejected bucket fails the WHOLE row, so an extra key would have made every older peer drop the row and keep a stale one, fleet-wide. Bumping `EVENTS_SCHEMA_VERSION` does not rescue that — the acceptor compares against the current constant and would then reject the rows it had retained. The card also revised the Track 23A renderer contract explicitly, stating which prohibitions the new premise retires and which one SURVIVES (the cross-machine disjointness argument is independent of the counter shape)._

### Group 34: Coverage reporting ✓ Shipped (v0.12.50)

- Track 34A — _shipped (v0.12.50): the retro card reports coverage the wire had been carrying unread. Three signals were connected: `degraded_sources` (on the wire since v0.12.47 with zero readers), `git_capture` (shipped in Track 30A, unread by the aggregator — which discharged the separately-filed E6), and `usageIsIncomplete` (thrown away at Grok cache normalization). A new coverage state landed between `complete=True` and `complete=False` on `HostUsageResult`: 3 of 200 Grok turns carry `usageIsIncomplete: true` and drop `costUsdTicks`, one of them the largest turn in the corpus, and the reader had been validating counter presence inside `usage` without ever inspecting the key set — accepting those as complete accounting. The first `mm push` after upgrading re-walks already-cached Grok `updates.jsonl` files once via key-absence of `partial_days`. Coverage notes name a machine the reader may have to walk over to; the remedy is `mm diag` on that machine, never a bare `mm push`._

  **Caveat resolved:** Group 31's carried-forward caveat (`degraded_sources` reaching the wire but never the card) closes here.

### Group 35: Host pricing ✓ Shipped (v0.12.52)

- Track 35A — _shipped (v0.12.52): Codex tokens are counted without double-counting the cache, and the retro prices them as an API list-rate equivalent per machine. Host readers had been mixing two counter schemas — Codex and Grok CLI report inclusive `input` (cache-read already inside it), OpenCode and Claude are disjoint — which priced naively was a 7.40x overstatement, and had made `## Agent activity` ~1.97x high since v0.12.36. Inclusive extractors now emit disjoint buckets and mark them with an additive `counter_semantics: "disjoint-v1"` sibling; key-absence means legacy, and only the exact known value is priceable, so a legacy peer renders `—` rather than a confident wrong number. `PRICING_FAMILY_BY_MODEL` + `VENDOR_FAMILY_TIERS` add literal four-field OpenAI cards for the four observed `gpt-*` families with `resolve_prices` gaining exactly one branch, deliberately NOT derived from `_tier` (whose cache multiples are an Anthropic billing property). `PRICING_LAST_UPDATED` split per vendor with the authoritative URL recorded beside each table._

  **xAI rates were HELD by gate decision, not omitted** — Grok had never completed a scan on this fleet. Of the two blockers named at the time, one (the OpenCode `$.id` defect) was **dissolved rather than fixed**: Group 36 removed OpenCode entirely on 2026-09-01. The remaining blocker is the `_validated_grok_entry` `offset == size` wedge, owned by Track 46A (the cache-encoding card — numbered 37A at `727f9cd`, 40A after the 2026-09-01 regen; this entry's original "Track 38A" was wrong at birth, colliding with the then-forensics card).

### Group 36: Three hosts ✓ Shipped (v0.12.53 + #156)

- Track 36A — _shipped (v0.12.53): the OpenCode usage reader is deleted rather than repaired. The reader had never returned a row against a live census (0 of 42 assistant rows — `$.id` projected via `json_extract` while OpenCode keeps `id` as a table column) and was the only reader built against a synthetic fixture. `ACTIVE_HOST_READERS` split from the wire tuple; the generic multi-reader isolation/merge/expiry contracts were ported onto a synthetic third reader so Codex and Grok keep their protection; the OpenCode fixture tree and its CONTRACT.md went with it. Net -475 lines._
- Track 36B — _shipped (#156 `999d54b`, deliberately not release-bearing, riding the v0.12.53 train — it landed 18 minutes before that release's commit): a legacy peer keeps publishing accepted host rows after OpenCode is gone — but NOT by the card's mechanism. The card pinned `"opencode"` inside `HOST_USAGE_TOKEN_SOURCES` forever; what shipped instead makes the acceptor tolerate ANY unknown source name: `_token_sources_subsequence` retains names outside the live universe when they pass an explicit identifier bound (`_TOKEN_SOURCE_ID_RE` — needed once the closed vocabulary stopped bounding strings for free), while duplicates and known-name-out-of-order stay fatal, and `_accept_optional_source_list` applies the same rule to `degraded_sources` and `partial_sources`. That generalization also pre-discharged the tolerance-widening instruction Track 50A used to carry. The wire tuple is now honestly `("codex", "grok")`. Prose swept across README, SPEC, AGENTS.md, host-parity, grok-build-usage-reader and the retro SKILL.md; the surviving OpenCode mentions are deliberate (legacy-peer explanations and README's link-check loop, annotated in place as an explicit exception)._

### Group 37: Verification contract ✓ Shipped (v0.12.54)

- Track 37A — _shipped (v0.12.54): mind-meld has one portable verification command. `./bin/check` (POSIX launcher + 586-line stdlib driver `bin/_check.py`) selects a supported interpreter (prefer 3.13, notice otherwise, never `xcode-select`), bootstraps only virtualenvs it owns (owned-venv marker + fcntl lock + content-hash dependency freshness, `MM_PYTHON` / `MM_VENV` / `VIRTUAL_ENV` handling), runs Ruff before pytest, and uses pytest-xdist when available with serial/debug escape hatches. CI runs the same command via `--no-bootstrap`; macOS Keychain validation and the isolated wheel smoke stay CI-only. `release.yml` advances `latest` only when the version tag resolves to the pushed commit, closing the untagged-`pyproject.toml`-edit hole. Width-coupled Rich test assertions fixed rather than pinning `COLUMNS`. Optional Conductor setup hook pre-warms the lint env in the background and exits 0 on failure. User decision 2026-09-02: one Track / one PR, superseding the six-Track split; review residue in `docs/designs/track-37A.md`. Shipped at 1,716 insertions against the card's ~800 — the M estimate had been carried through two reshapes while the requirement list grew to 14 items, and the PROGRESS row convention held (row in the PR, CI-enforced)._

### Group 44: Source retirement ✓ Shipped (v0.12.55 + v0.13.0)

- Track 44A — _(carded as Track 37B until the 2026-09-02 split from Group 37) shipped as two PRs after review (2026-09-01) rejected the carded `load_config` injection on three proofs and reused `mm migrate-config` instead — the "retired third source state" and the injected-`disabled_sources` machinery were never built. **v0.12.55 (#157)** retires the sync source: the `opencode` entry left `DEFAULT_SOURCES` and init's auto-detect; the next interactive push/pull/recapture offers `mm migrate-config`, which removes a leftover `[[sync.sources]]` opencode block and records the name in `disabled_sources` in one patch so the following push mints NO deletion tombstones (the `_filter_disabled_sources` P0 footgun honoured); auto-commands never mutate config; `mm enable-source opencode` refuses through the ordinary unknown-name path; `mm status` dropped the un-actionable "run mm enable-source" breadcrumb. A user who wants to keep syncing the directory renames the source (`name = "opencode-local"`), documented in README. **v0.13.0 (#158)** drops the `opencode` row from `skill_link.AGENT_ROWS` (the card's "BREAKING marker + `### Migration` CHANGELOG section" landed differently: the breaking signal is the MINOR bump itself, and the interactive `mm migrate-config` prompt stands in for a Migration section — a deliberate deviation, like the never-built third state): mm removes the `~/.config/opencode/skills/retro-fleet` link **it** created (a `readlink` equal to the mm-owned store) and leaves user-made files, dirs, and foreign links alone — the Track 28A deletion-guard distinction held; `_real_guard_paths` still refuses test writes to the retired path; a `[skills] agents` naming only retired agents emits a notice instead of silently declining; interactive `mm push` runs the reaper even when the 24h drift gate is shut (autopush does not). The two derived `next()` marker constants that would have raised `StopIteration` at import were dissolved with the row, as the card required._

### Group 45: Conflict sidecar clock and era safety ✓ Shipped (v0.14.0)

- Track 45A — _shipped at f2624fb (v0.14.0, #161), re-scoped from the 2026-09-03 forensics card: sidecar birth and era come from the filename, while st_mtime remains the peer clock. The suffix carries v1 after the UTC timestamp; migration uses the final infix and does not reinterpret peer age as era. GC preserves live, missing-canonical and v0 copies; only a redundant converged copy is reapable. Bare --dry-run previews conflicts, deletion lists paths, status reports unresolved conflicts, README states the post-inversion direction, and pull rejects conflict-shaped names from peers._

  **Scope reconciliation (2026-09-05):** this records the fix that actually shipped, not proof of which process removed all 25 historical copies. The original immediate post-write exists() warning did not ship and is retired: it cannot detect a later deletion or prevent either newly reproduced failure. Full-review found two still-open replacement defects — cross-canonical prefix matching and deleting old copies before a failed write — now Track 48A (2026-09-05 numbering). The old card's assertion that the prefix glob could not touch another canonical was false. No claim that all sidecar-loss mechanisms are fixed.

### Group 46: Grok ingestion recovery ✓ Shipped (v0.14.1)

- Track 46A — _shipped at 8be81ce (v0.14.1), re-scoped from the 2026-09-03 cache-encoding card: the reader recognizes the observed harmless elapsed_ms key and preserves its latest failure reason for cache-only status/diag, so a successful historical scan no longer hides a current permanent Grok read failure. Successful recovery clears the reason; a transient failure does not erase an earlier permanent one._

  **Scope reconciliation (2026-09-05):** neither per-state increment encoding nor an entry cap shipped. The 2026-09-04 live probe measured 4.11 MB / 23.3 ms, below the original 25 MB / 100 ms trigger, and found no live entry rejected by the alleged offset-equals-size wedge. Cache encoding, intra-file resume, states caps and reader-wide quarantine remain explicit Future work with their own triggers. Grok pricing remains open; the actual ingestion repair removes the false cache-rewrite prerequisite.

### Group 47: Sync surface ✓ Shipped (v0.14.0)

- Track 47A — _both tasks shipped at f2624fb (v0.14.0, #161): .extend-root-aware traversal skips generated directories using normalized path prefixes, preserves configured include roots, and suppresses deletion tombstones for skipped paths. The gstack source excludes projects/*/pair-review/session.yaml while keeping cross-machine prose artifacts in scope. Pull strips .extend-root and conflict-shaped paths independently of configured excludes. Marker-skip and tombstone behavior are pinned in manifest and integration tests._

### Group 48: Hotfix: Preserve conflict copies ✓ Shipped (v0.14.2)

- Track 48A — _shipped at 09d98ab (v0.14.2): conflict reuse, replacement and explicit-file discovery match the exact original filename (repeated conflict markers, literal glob characters, differing extensions), so a conflict for one file can no longer hide or delete another file's copy or route its bytes to the wrong resolver target. Failed replacement writes leave the local file and the previous peer copy intact; older same-peer copies are removed only after the new copy is saved and their contents were read. Replacement names are checked against files, directories and dangling links across each of five random fallback attempts. Sidecars whose names reconstruct to an empty filename or a `.`/`..` component are ownerless and preserved by listing, resolver and GC; an unreadable candidate no longer aborts discovery. `_canonical_for_conflict` moved into `manifest.py` as the card required, and the module-boundary test gained the pin. Shipped at 1,481 insertions against the card's ~140 LOC, widening onto SPEC, README, AGENTS.md, `tests/test_module_boundaries.py` and `tests/test_silent_failure_contract.py`._

### Group 49: Snapshot integrity ✓ Shipped (v0.14.3)

- Track 49A — _shipped at 0c1a969 (v0.14.3, #165): pushes publish complete snapshots or refuse. Publishing scans enumerate selected sources strictly; a permission, I/O, vanished-entry, descriptor-identity or marker-discovery failure refuses the whole push and keeps the previous encrypted manifest, recovery sidecar and last_seen. A still-present file omitted only for `max_file_size` or an inode alias refuses instead of minting a tombstone. `_upload_changed_blobs` verifies digest, size and mtime against the scanned revision before encrypting; missing input raises instead of counting as a skip. Pull hashes decrypted plaintext after the path/symlink guards and before apply, failing the one file and continuing. Interactive push reports `SnapshotError` with source, cause, "Previous snapshot kept" and a next action (exit 1); autopush keeps exit 0 with a typed stderr line and a failed breadcrumb. Removing a source from selection no longer invents removal tombstones. `mm push --dry-run` is a strict scan and deletion-proof preview only. Shipped at 3,168 insertions against the card's ~200 LOC: `config.py` (a shared `resolve_sources` helper), `errors.py` (`SnapshotError`, `snapshot_refusal`) and six test modules outside the declared footprint (test_config.py, test_safe_str.py, test_silent_failure_contract.py, test_skill_link.py, test_source_toggle.py, test_track_30a.py; corrected 2026-09-06 /ship review — `git show --stat 0c1a969` lists 9 changed test modules against 3 declared, not 7 undeclared)._

### Group 50: Terminal-safe recovery warnings ✓ Shipped (v0.14.4)

- Track 50A — _shipped at cc22b6c (v0.14.4, #166): both rejection warnings in `LocalBackend.find_conflict_copies` (validator false, validator raised) sanitize the full candidate path and the `ClassName: message` text before stderr, and `mm gc`'s malformed-blob-key and verbose-orphan warnings escape nonprintables first and Rich markup second; the original `Path` and key still drive validation, recovery and deletion. Added `safety.safe_terminal_str`, the single-line plain-stderr helper (strip, then `ascii()`-escape every non-printable). Warnings carry the `mm: warning:` prefix, say the file was left in place, and stop pointing at `mm gc --conflicts`, which never reaped rejected siblings. Widened onto `cli.py` and `safety.py` beyond the declared `storage/local.py`. Left behind as a recorded follow-up (the "Terminal-control postcondition" Track, 52A in the 2026-09-06 plan): one strip pass is not a no-control guarantee for the Rich and multiline sinks._

### Group 51: Deterministic JSONL merge ✓ Shipped (v0.14.5)

- Track 51A — _shipped at 38222ac (v0.14.5, #167): `merge_jsonl` sorts only records whose decoded JSON is an object with a string `ts` (the empty string included), by `(ts, original line)`; numbers, bools, null, arrays, non-object JSON, missing keys, malformed text, decoder `ValueError` and `RecursionError` all keep the original normalized line in a whole-line lexical fallback after the string bucket, and lines are never reserialized. An identical retry takes the merge no-op path (bytes and mtime untouched, no second `merged` row). Numeric-only `ts` files now sort as text, and an old client still crashes on mixed types, so a mixed-version fleet can rewrite such a file on each pull until every Mac is upgraded; README's troubleshooting section walks through `mm --version` / `pipx upgrade mind-meld`. README, SPEC and conflicts.md state the ordering contract; `MEMORY.md` is documented as a plain lexical line-union. Shipped close to the card: merge.py, two test modules, and docs._
