# Future

<!-- Deferred work. Maintained by /roadmap. Do not put Phase/Group/Track structure here. -->

## Future

- **cli.py micro-cleanups** — retain only the unresolved `_resolve_mm_events_dir` and status-enum follow-up; Track 18A owns the import and `_empty_outcomes` portions.
- **`_resolve_interactive_loop` decomposition** — collector removal fired its old trigger, but the 630-line function still needs a dedicated discovery/design pass before code commitment.
- **Two-machine test bootstrap duplication** — consolidate the repeated setup only when adjacent test work touches both modules.
- **Cold-cache budget leftovers** — revisit identity-refresh and per-jsonl deadline concerns only with fresh measurements; root discovery is shipped.
- **identity.py micro-DRY and token-cache pins** — keep as low-priority hygiene.
- **v0.11.17 doc-drift cleanup** — re-evaluate only with an invariant-doc pass.
- **Incremental-resume accepted divergences** — act only on evidence from a corpus census.
- **Future-clamped peer mtime** — advisory watch item.
- **`_promote_target_will_sync` ignores `exclude_patterns`** — rare exclude-glob miss.
- **Similarity classifier and silent merge** — blocked on a real collector dataset.
- **Peers never resolved against can be mtime-skipped by the drain** — watch after Group 12's shipped fix.
- **Abort transactionality** — pre-existing torn-state concern.
- **Price-cache TTL split** — wire-format work that competes with token-cache ownership.
- **Model-ID variant suffix aliases** — defer until a real variant appears in a census.
- **Rendered-cost sanity ceiling** — presentation safeguard without an observed bad value.
- **Parallel blob fetch** — revisit after user-reported sustained slow pulls.
- **Selective sync** — wait for a user with a large-project filtering need.
- **Mtime hash cache** — revisit only if push latency becomes user-visible again.
- **Three-way merge base** — wait for a divergence-misclassification report.
- **`mm rekey` passphrase rotation** — post-1.0 format-v3 migration work.
- **Blob-directory peer recovery** — wait for a real corrupt-manifest support case.
- **PyPI publish workflow** — wait for a distribution-demand signal.
- **Cross-device source rename identity** — known limitation until an incident demonstrates it.
- **Explicit upgrade check** — add only when cached status is insufficient.
- **Subprocess pipx upgrade** — revisit after nudge UX proves inadequate.
- **`MM_NO_VERSION_CHECK=1`** — add only for demonstrated CI ergonomics.
- **GitHub-tag pagination** — deferred until tag count approaches the API page limit.
- **Autouse devices-write-lock coupling** — forward-defense only.
- **`[retro].deny_emails`** — wait for a credential or account-hygiene need.
- **Snapshot-level completeness** — separate data-model design; do not conflate with host snapshots.
- **Retro-card machine/cause diagnostics** — defer until the baseline model card lands.
- **`mm diag` discoverability and analytics** — keep scoped to the existing command rather than adding a new one.
- **Diagnostic-string quality pass** — adopt only with a broader user-facing notice pass.
- **Per-verb or sticky autorun breadcrumbs** — the v0.12.16 signal is an improvement but still overwritable.
- **CT-4 enforcement and short-write handling** — storage-boundary hardening outside this batch.
- **Residual Track 16A coverage gaps** — three defensive or theoretical cases from the ship audit. _Source: [ship] coverage audit 2026-08-15._
- **Host-usage cache GC reaper** — extend `mm gc` and its dry-run path to remove stale Codex, Grok, and OpenCode cache entries without weakening complete-pass pruning. _Source: unprocessed host-cache follow-up 2026-08-17._
- **Active host-session degradation policy** — consider skipping a stale or partial final rollout when its next completed record restates usage; preserve all-or-nothing publication until that proof exists. _Source: unprocessed host-usage follow-up 2026-08-17._
- **Warm host-scan scaling** — revisit fingerprint-every-file cost with a measured corpus before the 250 ms autopush budget becomes user-visible. _Source: unprocessed host-cache follow-up 2026-08-17._
- **Machine-readable GC outcomes** — expose orphan-blob outcomes only when an automation or audit consumer needs them; Track 17D's reaper scope stays as shipped. _Source: unprocessed GC follow-up 2026-08-17._
- **Do not add a Codex or Grok sessions-snapshot** — Claude's sessions-snapshot walk stays Claude-only. Codex rollouts and Grok session dirs are not a metadata-only project ledger; encoded cwd is a path and must not go on the wire. Promote only if a host ships a metadata-only project index. Session-transcript sync stays refused for every host. _Source: [manual] host-parity 2026-08-17._
- **Deterministic demo/fixture path for `retro-fleet`** — fresh-clone time-to-first-output is 10-30 min and nondeterministic (3.11+ interpreter, venv, editable install, `mm init`, an enabled host source, a substantive push, two aggregator passes). A `--demo` flag over a bundled synthetic corpus would make the card reproducible in three commands. The test suite is the current deterministic path and is adequate for CI, so this is ergonomics, not correctness. _Source: [23A] inbox 2026-08-18._
- **Machine-readable retro metrics export** — add `mm retro-fleet --format json` only when a structured consumer needs it. The removed v0.12.0 snapshot JSON was the de-facto export surface; a scheduled `mm retro-fleet 7d --format json >> ~/retro-history.jsonl` would be a better long-horizon archive. _Source: [24B] T12 / DX-6 2026-08-22._
- **Bounded binary retro-event JSONL reader** — `_iter_jsonl` currently uses unbounded text iteration with replacement decoding, unlike `token_usage.iter_bounded_lines`. Preserve the one-materialisation prior-period design while making malformed-input handling bounded and byte-safe. _Source: [24B] T12 / Eng F1 2026-08-22._
- **Un-hide and rename `--dump-host-usage`** — hidden from `--help`, and documented only in CHANGELOG and the maintainer-facing invariant doc, so the primary forensic hatch is undiscoverable, and the name reads like spend when it is retained inventory. Prefer `--host-inventory-json`, keeping the old flag as an alias. _Source: [23A] inbox 2026-08-18._
- **Accept a bare integer retro window** — `mm retro-fleet 7` is rejected while `7d` works. The skill translates natural language so agents never hit it; direct CLI users do. _Source: [23A] inbox 2026-08-18._
- **Deregister/prune retired devices** — a retired-but-registered Mac inflates every "N of M machines" denominator forever, which is the root cause 23A's coverage wording worked around rather than fixed. Wants `mm devices --prune` or a staleness nudge. _Source: [23A] inbox 2026-08-18._
- **Reset-aware per-device snapshot deltas** — the only honest route to real per-agent window spend, versus the lower-bound day counts 23A ships. Needs at least two retained snapshots per device (22A keeps only the latest), counter-reset detection, and a new wire/consumer invariant. A data-layer track, not a renderer change. _Source: [23A] inbox 2026-08-18._
- **Two releases share the version string `0.11.23`** — `CHANGELOG.md` carries `## [0.11.23] - 2026-05-06` and `## [0.11.23] - 2026-05-05` as separate releases; `docs/PROGRESS.md` charts only the 2026-05-05 one, so the 2026-05-06 "auto-pin iCloud storage on `mm init`" release has no row. `test_every_changelog_version_has_a_progress_row` matches on version string, so the gate cannot see this and reports parity. Needs a decision: renumber one release, or teach the gate to key on (version, date). _Source: /ship adversarial review 2026-08-21._
- **Fleet-wide skill-link visibility** — markers and links are per-machine, so nothing answers "which of my Macs has a wedged link", which is the real question behind surfacing a wedge. One field on the mm-push event would let the retro card answer it. Deferred as a wire-format change, outside the installer's blast radius. _Source: /autoplan Phase 3 eng review 2026-08-20._
- **install-skills `--dry-run`/`--force`/`--json`** — both DX voices wanted this surface (`--force` renames to `retro-fleet.mm-backup-<ts>`, never deletes; exit 3 when `--dry-run` would change something; no `--migrate-legacy`; no `--store PATH`; `MM_SKILLS_DIR` for tests). Parked so 25B stays markdown-only and can room with the registry. 24A already makes `dry_run=True` return full classifications. _Source: /autoplan Phase 3.5 DX X7 2026-08-21._
- **Race-safe skills-dir ancestor pin (`dir_fd` + `O_NOFOLLOW`)** — Approach B makes dangling repair and the `lockedjson` ownership ledger unnecessary; this POSIX leftover from old 29A is hardening, not a wedge fix. _Source: /autoplan Phase 3 eng E1 correction 2026-08-21._
- **`SkillInstallResult.action` field** — the enum still mixes observed state with outcome (`installed` cannot distinguish created/migrated/repaired). `_failed_result` still throws `operation` away. A store-publish failure should render as one failure, not three. _Source: /autoplan Phase 3.5 DX X9 2026-08-21._
