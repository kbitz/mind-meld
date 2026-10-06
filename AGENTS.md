# AGENTS.md

## Project
Mind Meld (mm) — CLI for syncing AI coding-agent context, skills, and gstack activity across Macs via iCloud Drive. Supports Claude Code, Codex, Grok, and configurable sync sources, plus Cursor usage capture.

<!-- roadmap:parallelism_cap=8 -->
Up to eight Tracks can run across separate workspaces; the host owns branch and worktree lifecycle.

## Stack
Python 3.11+, typer, cryptography, argon2-cffi, keyring, rich, packaging.

## Key Principles
- No API server. The CLI talks directly to iCloud Drive through the local filesystem.
- Single storage backend: `~/Library/Mobile Documents/com~apple~CloudDocs/mind-meld`.
- **End-to-end encryption is mandatory.** All synced manifests, artifacts, and allowlisted context are encrypted client-side with AES-256-GCM before touching storage. No plaintext sync data may reach storage.
- **Scoped sync.** Built-in sources are allowlisted. Session databases, credentials, and whole-file settings that may contain credentials stay local.
- **Truth-based manifests.** Complete snapshots of local state propagate deletions automatically; there is no separate prune step.
- **Conflicts preserve local canonical bytes and remote sidecars.** Newer local files are skipped; legacy direction and clock rules live in the conflict invariants.
- After pull, `.mind-meld-log.md` records changes from other machines per project.
- SHA-256 manifest diffing transfers only changes; blobs are content-addressed and gzip-compressed before encryption (format 0x02).

## Planning

Admission gates: [docs/ROADMAP.md](docs/ROADMAP.md); rationale: [constraint history](docs/roadmap-shipped.md#planning-constraint-history).

- Verify premises against current HEAD and cited artifacts this turn. Discharge delivered goals; kill disproved or obsolete work orders.
- Establish demand before pricing repairs. One Mac's cold artifact proves only that Mac's inactivity; repair or retirement must follow supported product scope.
- Investigate unwanted automatic behavior before adding its inverse; removal controls need demonstrated demand.
- Name a reader for every persisted field, or the delivering Track and its title.
- Trace normalization, retained cache fields, and migration gates when changing readers.
- Prove inclusive/disjoint counter semantics for every reader before pricing, summing, or trending; see [events/retro invariants](docs/invariants/events-retro.md).
- Read the roadmap audit's effective SIZE limits. Machine-local caps come from installed gstack-extend's `bin/config get roadmap_max_files_per_track` and `bin/config get roadmap_max_session_weight`, with environment overrides. Historical values from another Mac are not project limits.

## Before editing code

Before editing anything under `src/`, read [docs/invariants/README.md](docs/invariants/README.md), find the file or function you're touching, and read every invariant doc it names. This is the authoritative module map and routing table. Shortcut: `rg -n '<symbol>' docs/invariants/`, then read every doc named by matching README rows and every doc whose header lists it.

## Cross-cutting rules

- Imports flow from `cli` to extracted modules, never back; leaves import nothing from the CLI layer. Call moved symbols module-qualified (`resolveflow.foo(...)`), not via from-import, so owner patches reach callers. See the routing README for the exact module list; `tests/test_module_boundaries.py` enforces it.
- `aggregator.py` reaches the CLI only as a subprocess, never an import.
- Build storage keys only through validated helpers in `storage/keys.py`, never raw f-strings.
- Use `lockedjson` for flock-guarded JSON caches; extend it instead of adding ad-hoc fcntl. The multi-file `devices-write.lock` is the exception.
- Sanitize peer-controlled display strings through `safety`; use `safe_terminal_str` for single-line plain stderr. Display text is not a filesystem identity or shell argument.
- Never quiet-gate data-at-risk warnings. Normalize config failures at `load_config`; record every events-tail degradation in `PushResult.events_degradations`, not just stderr.
- Do not reintroduce the conflict collector or its cancelled auto-resolver; see [conflict invariants](docs/invariants/conflicts.md#collector-removal-and-auto-resolver-cancellation).
- Release PRs include the `docs/PROGRESS.md` row with the `pyproject.toml` and `CHANGELOG.md` bump. Never restore workflow pushes to protected main. Row format and release rules: [auto-upgrade invariants](docs/invariants/auto-upgrade.md#release-discipline-enforced-by-mm-auto-upgrade).
- Every `_get_config` and `_maybe_prompt_migration` call requires `read_only=`. Update integration tests' `COMMAND_INTENTS62` for every new command; previews and inspection must preserve their write boundaries.

## Testing

`./bin/check` is the verification entry point: bootstraps `.venv` if needed, then runs `ruff check .`, `ruff format --check .`, and pytest (default scope `tests/`). Use `tmp_path` for local backends.

```
./bin/check                         # full portable checks
./bin/check tests/test_config.py    # scoped pytest; still lints the whole repo
```

`--tests` skips lint; `--lint` skips pytest. Ruff is pinned in dev dependencies and enforces E/F/W/I. Every roadmap card's `verify:` field must use `./bin/check <scope>`; cards must not encode where Python lives. The `PYTEST_CURRENT_TEST` guard on `crypto.store_passphrase_in_keyring` is load-bearing; see [init/device invariants](docs/invariants/init-devices.md).

## CI

`.github/workflows/ci.yml` runs portable checks plus real Keychain and isolated wheel smokes on macOS/Python 3.13; see [README Development](README.md#development). Local portable checks do not qualify those CI-only checks. Release automation and compatibility rules live in [auto-upgrade invariants](docs/invariants/auto-upgrade.md). Version source: `pyproject.toml`; no `VERSION` file.

## Commands

mm --version | init | push | pull | status | diag | devices | diff | gc | sources | conflicts | resolve | recover | log | migrate-config | autopull | autopush | enable-source | disable-source | reconfigure-sources | refresh-identity | install-skills | retro-fleet | recapture | update | cursor-agent (plus the hidden `capture-cursor-usage` hook)

Attended `mm push` refreshes consented host usage under the lock. Exit 0 means content sync succeeded regardless of capture outcome; verify recorded capture/publication with `mm status`. Autopush stays change-gated and never warms; previews never capture. Flags and exit codes: README and SPEC.

## Auto Commands

- `autopull` / `autopush` never prompt, are quiet on the happy path, and exit silently when uninitialized or unchanged.
- Malformed config emits a one-line stderr error; data-at-risk warnings remain visible in quiet mode. Unexpected errors degrade gracefully.
- `push`/`pull`/`autopull`/`autopush` upgrade a pipx `@latest` install at their tail (`auto_install`, default on; previews skip it); failures never change the exit code.
- `autopull` reports per-file apply failures and counts. `autopush` records `no-sources` and `degraded` breadcrumbs so `mm status` can expose failures.
- Detailed contracts: [sync](docs/invariants/sync.md), [events/retro](docs/invariants/events-retro.md) and [auto-upgrade](docs/invariants/auto-upgrade.md) invariants. Integration snippets: README.

## Where detail lives

- [Module map and invariant routing](docs/invariants/README.md): authoritative current source layout and mandatory per-topic rules.
- [README](README.md): commands, configuration, previews, integration, and development.
- [SPEC](SPEC.md): architecture and data model; Project Structure, Implementation Order, and original push algorithm are historical.
- Design decisions: [v1](docs/designs/mind-meld-v1.md), [multi-source sync](docs/designs/sync-gstack-context.md), [host parity](docs/designs/host-parity.md), [Grok reader](docs/designs/grok-build-usage-reader.md).
- [ROADMAP](docs/ROADMAP.md): execution plan (edited only by /roadmap); [TODOS](docs/TODOS.md): deferred work; [PROGRESS](docs/PROGRESS.md): release rows. Superseded designs live in `docs/archive/`.
