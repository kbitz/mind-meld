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
- 5 sessions
- Tokens this window: 1.1M in / 7.0M cache_read / 200.0k out
- Cache hit ratio:    63%
- Estimated cost:     ~$47.40 (Fable 5.1 $30, Sonnet 5 $17)
- Per-model:          Fable 5.1, Sonnet 5
- *List pricing last verified 2026-08-11. Cost estimates do not account for subscription plan pricing.*

## Skills used (0 invocations)
- No skill invocations captured.

## Agent activity

Per-machine per-turn counters; never safe to sum across machines (host stores move by OS migration, so two device ids can hold one history).

| Machine | Model family | Snapshot (UTC) | State | Tokens (last 90 active days) | Tokens in this window |
|---|---|---|---|---|---|
| dev-a | Codex models | 2026-09-14 | current | 3.2M | 3.2M |
| dev-a | Grok models | 2026-09-14 | current | 1.1M | 1.1M |
| dev-b | Claude (via agents) | 2026-09-14 | current | 2.0M | 2.0M |

- Readers per machine (`none` = no reader contributed): dev-a codex, grok; dev-b codex.
- *A peer on an older mm still reports last-touch totals rather than per-turn ones, so its token columns overstate the recent edge and its day counts are lower bounds; a machine that never pushed in this window contributes no days either. Counters cover at most the 90 most recent active UTC days.*

## API list-rate equivalent (per machine)

- Anthropic list rates, verified 2026-08-11: https://platform.claude.com/docs/en/about-claude/pricing
- OpenAI short-context list rates, verified 2026-09-10 against https://developers.openai.com/api/docs/pricing
- xAI base and long-context list rates, verified 2026-09-10: https://docs.x.ai/developers/models/grok-4.6

Historical usage is repriced at current rates: the rates bundled with this mm release, verified on the dates above. Not subscription spend. Cost estimates do not account for subscription plan pricing.

- ``~``: estimate from the recorded tokens and bundled rates.
- ``>=``: floor; at least one of: unpriced models, a host reader that declared incomplete totals, a dropped reader, or tokens the per-day model cap left unattributed, or a model whose long-context tier cannot be reconstructed.
- ``—``: the figure is unavailable, not zero.

### Do not sum these values

Machines may hold duplicated history (OS migration, a fresh `mm init`) and these values must not be summed.

| Machine | API list-rate equivalent |
|---|---|
| dev-a | >=$20.25 |
| dev-b | >=$15.00 |

## mm sync activity
- 0 pushes across 0 device(s)

## Notes
- API list-rate equivalent for `dev-a` is a floor (>=): 1 unpriced model(s) (grok-unknown); upgrading mm on the machine that renders this report may price it; republishing does not add a rate; do not estimate; Grok's logs do not record per-request prompt sizes; no action resolves this (`grok-4.6-build`: $2.00 at the base tier, at most $4.00 at the long-context tier for this model's recorded tokens, in token charges; server-side tool fees excluded).
- API list-rate equivalent for `dev-b` is a floor (>=): a host reader failed (grok).
- Tokens incomplete on dev-a: pre-v0.11.14 OR cold token cache — run `mm push` on those machines; upgrade if the warning persists for accurate token totals.
- Skills incomplete: 1 peer(s) on pre-v0.11.27 OR with cold token cache — upgrade and/or run `mm push` on those machines for accurate skill totals.
- Host-usage reader(s) grok failed on the latest push from dev-b — on `dev-b`, run `mm diag` and inspect `host_usage.grok`.
