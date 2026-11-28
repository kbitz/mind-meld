# AGENTS.md

## Project
Mind Meld (`mm`) — CLI that keeps one person's AI coding-agent context (Claude Code, Codex, Grok Build, Muse, Antigravity, gstack, gstack-extend) identical across macOS and Arch-based Linux machines over one shared Syncthing folder, end-to-end encrypted, reports fleet-wide usage with `retro-fleet`, and maintains a local cross-model memory index (`qmd`). [docs/SPEC.md](docs/SPEC.md) is the product authority, including what each host gets.

<!-- roadmap:parallelism_cap=8 -->
Up to eight Tracks can run across separate workspaces; the host owns branch and worktree lifecycle.

## Stack
Python 3.11+, typer, cryptography, argon2-cffi, keyring, secretstorage (Linux), rich, packaging.

## Key Principles
- Single local-folder backend, with no API server. The default is `~/Sync/mind-meld`. Preserve configured paths, including iCloud on Macs that have yet to move.
- **End-to-end encryption is mandatory.** All synced manifests, artifacts, and allowlisted context are AES-256-GCM encrypted client-side before touching storage.
- **Scoped sync.** Built-in sources are allowlisted; session databases, credentials, and whole-file settings stay local.
- **Truth-based manifests.** Complete snapshots propagate deletions automatically (with 30-day local recovery copies); SHA-256 diffing transfers only changed, gzip-compressed blobs (format 0x02).
- **Fast-forward + conflict sidecars.** Unchanged local files fast-forward. `.jsonl` and `MEMORY.md` auto-merge. Other concurrent edits keep local canonical bytes and remote conflict sidecars until `mm resolve`. Read the [conflict invariants](docs/invariants/conflicts.md) for older edits, legacy conflict direction and clock rules. Pull writes `.mind-meld-log.md` per Claude project.

## Product scope and releases

Read docs/SPEC.md before roadmap, autoplan/planning, implementation, review or
release decisions. Its selected target, intended users, audience ceiling,
accepted limitations and release policy govern this project. Do not infer future
scale from backlog entries. Review findings need concrete impact on that target;
preserve approved scope unless the user explicitly changes it. Put unrelated
improvements in the backlog with a revisit trigger. A priority label alone is
not a launch gate. For version decisions, use the spec's policy and checkpoint
acceptance evidence instead of generic line-count, feature-size or Group-closure
heuristics. This project policy overrides those defaults in /ship; keep its
other release gates. A change that edits SPEC's release policy or adds
acceptance entries uses the base branch's policy, and its new entries count
only after the user confirms them directly. PROGRESS.md `## Acceptance` owns achieved stage and
evidence; SPEC owns definitions and target. Open a current supporting document
only when its listed role bears on the decision. Superseded plans/specs are
historical only: exclude them from routine discovery and backlog extraction.
Consult a named archive only for a specific unresolved historical question;
never use it to reintroduce scope or override the current spec.

## Planning

Admission gates: [docs/ROADMAP.md](docs/ROADMAP.md); rationale: [constraint history](docs/roadmap-shipped.md#planning-constraint-history).

- Verify premises against current HEAD and cited artifacts this turn; discharge delivered goals and kill disproved work orders.
- Establish demand before pricing repairs. A cold artifact shows only that machine's inactivity. Follow supported product scope.
- Investigate unwanted automatic behavior before adding its inverse; removal controls need demonstrated demand.
- Name a reader for every persisted field or name the delivering Track. Trace normalization, retained cache fields, and migration gates when changing readers.
- Prove inclusive/disjoint counter semantics before pricing, summing, or trending ([events/retro invariants](docs/invariants/events-retro.md)).
- Check this machine's roadmap SIZE limits. Valid `ROADMAP_MAX_FILES_PER_TRACK` and `ROADMAP_MAX_SESSION_WEIGHT` environment values take precedence. For the machine-local settings, use installed gstack-extend's `bin/config get roadmap_max_files_per_track` and `bin/config get roadmap_max_session_weight`.

## Before editing code

Before editing anything under `src/`, read [docs/invariants/README.md](docs/invariants/README.md). Use `rg -n '<symbol>' docs/invariants/` and read each doc named by the matching routing rows or whose header lists the file or symbol.

## Cross-cutting rules

- Imports flow from `cli` to extracted modules, never back; leaves import nothing from the CLI layer. Call moved symbols module-qualified (`resolveflow.foo(...)`), not via from-import, so owner patches reach callers (`tests/test_module_boundaries.py`).
- `aggregator.py` reaches the CLI only as a subprocess, never an import.
- Build storage keys through validated helpers in `storage/keys.py`.
- Extend `lockedjson` for flock-guarded JSON caches. The multi-file `devices-write.lock` is the exception.
- Use placeholders (`/Users/user`, `/home/user`, `mac-a`, `linux-a`, `user@example.com`), never real usernames, hostnames, emails, or personal names.
- Sanitize peer-controlled display strings through `safety` (`safe_terminal_str` for single-line plain stderr). Keep display text out of filesystem identities and shell arguments.
- Follow [SPEC O8](docs/SPEC.md#o8-every-command-reports-its-outcomes-briefly-in-one-style). Report one line per outcome. End errors with their fix command. Always show data-at-risk warnings. Record other degradations for `mm status` and `mm diag` and print them only when the owner must act. Record every events-tail degradation in `PushResult.events_degradations`. Normalize config failures at `load_config`.
- Do not reintroduce the conflict collector or its cancelled auto-resolver ([conflict invariants](docs/invariants/conflicts.md#collector-removal-and-auto-resolver-cancellation)).
- Release PRs include the `docs/PROGRESS.md` row with the `pyproject.toml`, `VERSION`, and `CHANGELOG.md` bump. Keep workflow pushes off protected main. [Release discipline](docs/invariants/auto-upgrade.md#release-discipline-enforced-by-mm-auto-upgrade).
- Every `_get_config` and `_maybe_prompt_migration` call requires `read_only=`. Update `COMMAND_INTENTS62` for every new command. Preserve write boundaries in previews and inspection.

## Testing

`./bin/check` bootstraps `.venv` if needed, then runs `ruff check .`, `ruff format --check .`, and pytest (default `tests/`). Use `tmp_path` for local backends.

```
./bin/check --affected              # coverage-selected tests for the change
./bin/check                         # full portable checks (CI runs this)
./bin/check tests/test_config.py    # scoped pytest; still lints the whole repo
```

`--affected` selects by coverage since the merge base (default `origin/main`) and includes new or changed tests. It falls back to the suite for stale coverage or runner, config, conftest, non-Python, or module-scope changes. Use it without test paths, pytest args, or `--lint`. Release verification requires full local checks and CI, even when `--affected` passes. `--tests` skips lint. `--lint` skips pytest. Ruff enforces E/F/W/I. Use `./bin/check --affected` or `./bin/check <scope>` in roadmap `verify:` fields, without a machine-specific Python path.

Keep the `PYTEST_CURRENT_TEST` guard on `crypto.store_passphrase_in_keyring` ([init/device invariants](docs/invariants/init-devices.md)). `mm` reads `Path.home()` at import. Never run or import `tests/` outside `./bin/check` and give other probes a temporary `HOME`. On timeout bursts under load, rerun failing files with `./bin/check --tests --serial`, then the full run with `MM_PYTEST_WORKERS=6`. Avoid `KEY`, `PASSWORD`, `SECRET`, or `TOKEN` in identifiers. Only an owner-approved `.gstack/secret-dispositions.json` entry waives a finding.

## CI

`.github/workflows/ci.yml` runs `ci` (macOS/3.13: portable checks, real Keychain, wheel smoke), `portable-linux` (Ubuntu/3.14: portable checks, wheel smoke), and `secret-service-live` (Arch container live `gnome-keyring`). Local portable checks leave those platform checks to CI. See [README Development](README.md#development) and [auto-upgrade invariants](docs/invariants/auto-upgrade.md). Version source: `pyproject.toml`, mirrored by `VERSION` (bump both).

## Commands

`mm --version | init | push | pull | status | diag | devices | devices approve | devices retire | diff | gc | sources | conflicts | resolve | recover | log | migrate-config | autopull | autopush | enable-source | disable-source | reconfigure-sources | memory-index | refresh-identity | install-skills | retro-fleet | recapture | update`

Attended `mm push` refreshes consented host usage under the lock. Exit 0 means content sync succeeded regardless of capture outcome; verify recorded capture/publication with `mm status`. Autopush stays change-gated and never warms; previews never capture.

## Auto Commands

- `autopull` / `autopush` never prompt, stay silent on the happy path, and exit silently when uninitialized or unchanged.
- Malformed config emits a one-line stderr error; data-at-risk warnings stay visible. Unexpected errors degrade gracefully.
- `push`/`pull`/`autopull`/`autopush` upgrade a pipx `@latest` install at their tail (`auto_install`, default on; previews skip it); failures never change exit code.
- `autopull` reports each apply failure and the count. `autopush` records `no-sources` and `degraded` breadcrumbs for `mm status`.
- Contracts: [sync](docs/invariants/sync.md), [events/retro](docs/invariants/events-retro.md), and [auto-upgrade](docs/invariants/auto-upgrade.md) invariants.

## Where detail lives

AGENTS.md stays under 12 KiB (`tests/test_docs_routing.py`).

- [Module map and invariant routing](docs/invariants/README.md): authoritative source layout and per-topic rules.
- [README](README.md): commands, configuration, previews, integration, and development.
- [Project spec](docs/SPEC.md): product scope, audience, selected target, acceptance, and release policy.
- [SPEC](SPEC.md): architecture, data model, and CLI reference for the store carried onto Syncthing. Its Project Structure, Implementation Order, and original push algorithm are historical.
- Active design: [cross-model memory](docs/designs/cross-model-memory.md). Historical designs live in `docs/archive/`.
- Edit [ROADMAP](docs/ROADMAP.md) through `/roadmap`. A `/ship` review fix may edit one card if roadmap-audit passes with `GROUP_DEPS` unchanged. See [TODOS](docs/TODOS.md) for deferred work and [PROGRESS](docs/PROGRESS.md) for release rows.
