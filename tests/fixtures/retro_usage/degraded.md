╔══════════════════════════════════════════════════════════════╗
║  Example · 2026-09-07 → 2026-09-14                           ║
╠══════════════════════════════════════════════════════════════╣
║  0 commits · 0 repos · 2 machines                            ║
║  +0 / -0 LOC                                                 ║
║  0 detected GitHub PR references                             ║
║                                                              ║
║  MODELS (Claude Code sessions)                               ║
║  Claude: 11.3M tokens                                        ║
║  Model-token coverage incomplete: 1 peer(s); see Notes       ║
║                                                              ║
║  AGENT LOGS (2 of 2 machines with agent activity)            ║
║  Claude (via agents): seen on 1 day                          ║
║  Codex models: seen on 1 day                                 ║
║  Grok models: seen on 1 day                                  ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝

# Retro: 2026-09-07 → 2026-09-14 (7d)

**Activity across 2 of 2 known machines**

## Code shipped
- 0 commits across 0 repos (deduped across machines)
- +0 / -0 LOC

## Claude Code activity

Source: Claude Code session logs; sum of per-machine inventories, not deduplicated (a migrated home directory can be counted twice). Tokens from 2 of 2 machines; newest contributing snapshot 2026-09-14T12:00:00+00:00; window 2026-09-07 → 2026-09-14 UTC days; coverage incomplete; see Notes.

- 5 sessions
- Cache hit ratio:    63%

API list-rate equivalent (Claude Code, window sum)

In = input; Cache w = cache write; Cache r = cache read; Out = output.

| Model | In | Cache w | Cache r | Out | List-rate $ |
|---|---:|---:|---:|---:|---:|
| Sonnet 5 | 1.0M | 2.0M | 3.0M | 100.0k | >=$8.60 |
| Fable 5.1 | 100.0k | 1.0M | 4.0M | 100.0k | >=$19.50 |
| All models | 1.1M | 3.0M | 7.0M | 200.0k | >=$28.10 |

Anthropic rates verified 2026-09-14; see the shared legend in API list-rate equivalent (per machine); these two figures come from different logs; never add them.

## Skills used (0 invocations)
- No skill invocations captured.

## Agent activity

Source: latest host-usage snapshots; per machine, never summed. Host logs can lose old records; observed endpoints do not prove continuous coverage. Window: 2026-09-07 → 2026-09-14 UTC days; observation and coverage per machine below.

| Machine | Family | As of UTC | State | Retained | Window |
|---|---|---|---|---|---|
| dev-a | Codex | 2026-09-14 | current | 3.2M | 3.2M |
| dev-a | Grok | 2026-09-14 | current | 1.1M | 1.1M |
| dev-b | Claude* | 2026-09-14 | current | 2.0M | 2.0M |

All token counts sum input, cache write, cache read and output. Claude* = Claude (via agents). State: stale = last seen before window; ahead = clock ahead (<=24h); idle = current, no agent activity observed; missing = no snapshot.

- dev-a: snapshot 2026-09-14T12:00:00+00:00; observed UTC days 2026-09-10 → 2026-09-10; historical coverage unknown.
- dev-b: snapshot 2026-09-14T12:00:00+00:00; observed UTC days 2026-09-10 → 2026-09-10; incomplete; see Notes.
- Readers per machine (`none` = no reader contributed): dev-a codex, grok; dev-b codex.
- *A peer on an older mm still reports last-touch totals rather than per-turn ones, so its token columns overstate the recent edge and its day counts are lower bounds; a machine that never pushed in this window contributes no days either. Counters cover at most the 90 most recent active UTC days.*

## API list-rate equivalent (per machine)

- Anthropic list rates, verified 2026-09-14: https://platform.claude.com/docs/en/about-claude/pricing
- OpenAI short-context list rates, verified 2026-09-10 against https://developers.openai.com/api/docs/pricing
- xAI base and long-context list rates, verified 2026-09-10: https://docs.x.ai/developers/models/grok-4.6

Historical usage is repriced at current rates: the rates bundled with this mm release, verified on the dates above. Not subscription spend. Cost estimates do not account for subscription plan pricing.

- ``~``: estimate from the recorded tokens and bundled rates. May use a family-extrapolated rate.
- ``>=``: floor of the priced subtotal under bundled rate assumptions, never a guaranteed billing minimum; causes include unpriced models, incomplete coverage, a dropped reader, unattributed tokens, or a model whose long-context tier cannot be reconstructed. See Notes.
- ``—``: the figure is unavailable, not zero.
- Anthropic cache writes use the 1-hour rate for estimates and the 5-minute rate for floors. Once a floor applies, every priced cell in that section uses floor rates.
- Fast-mode turns on Opus 5 / 4.8 bill at 2x and are priced here at standard rates.

Source: latest host-usage snapshots; per machine, never summed. Host logs can lose old records; observed endpoints do not prove continuous coverage. Window: 2026-09-07 → 2026-09-14 UTC days; observation times, observed day ranges and coverage are listed in Agent activity. API list-rate equivalent (per machine — do not sum).

### Do not sum these values

Machines may hold duplicated history (OS migration, a fresh `mm init`) and these values must not be summed.

| Machine | API list-rate equivalent |
|---|---|
| dev-a | >=$20.25 |
| dev-b | >=$11.25 |

### Largest priced models (per machine; does not sum to the row in general)

Top models by tokens, capped per machine; unpriced model cells are —.

| Machine | Model | List-rate $ |
|---|---|---:|
| dev-a | gpt-6-astra | >=$18.25 |
| dev-a | grok-4.6-build | >=$2.00 |
| dev-a | grok-unknown | — |
| dev-b | claude-opus-5 | >=$11.25 |

## mm sync activity
- 0 pushes across 0 device(s)

## Notes
- API list-rate equivalent for `dev-a` is a floor (>=): 1 unpriced model(s) (grok-unknown); upgrading mm on the machine that renders this report may price it; republishing does not add a rate; do not estimate; Grok's logs do not record per-request prompt sizes; no action resolves this (`grok-4.6-build`: $2.00 at the base tier, at most $4.00 at the long-context tier for this model's recorded tokens, in token charges; server-side tool fees excluded).
- API list-rate equivalent for `dev-b` is a floor (>=): a host reader failed (grok).
- API list-rate equivalent uses floor rates throughout the per-machine section: at least one machine has a floor condition described in Notes; every priced row and model subtotal uses the same minimum cache-write assumptions.
- Tokens incomplete on dev-a: pre-v0.11.14 OR cold token cache — run `mm push` on those machines; upgrade if the warning persists for accurate token totals.
- Claude Code API list-rate equivalent is a floor (>=): token coverage is incomplete; 1 of 3 selected projects (1 of 5 sessions) lack token data in v2 snapshots; pre-v2 peers are not measurable. Every priced Claude row uses floor rates; see Tokens incomplete for the remedy.
- Skills incomplete: 1 peer(s) on pre-v0.11.27 OR with cold token cache — upgrade and/or run `mm push` on those machines for accurate skill totals.
- Host-usage reader(s) grok failed on the latest push from dev-b — on `dev-b`, run `mm diag` and inspect `host_usage.grok`.
