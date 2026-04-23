# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

## Unprocessed

- **[plan-eng-review Track 1B] Stop mutating config in `_apply_defaults`; compute expanded paths lazily in `get_sources`.**
  - What: rework `_apply_defaults` so `load → save` round-trip preserves human-readable forms (e.g. `~/.claude` stays `~/.claude`). Expansion + `.resolve()` happen at use sites only.
  - Why: backfill save at `cli.py:227-233` silently rewrites user's TOML from `~/.claude` to the canonical absolute path on any first-run-after-upgrade. Codex flagged this during /plan-eng-review 2026-04-23 as a UX footgun. Track 1B's `.resolve()` addition extends the footgun to symlink dereference; the proper fix is to not mutate config at load time at all.
  - Pros: no surprise path canonicalization in saved config; TOML stays close to user intent.
  - Cons: requires updating all readers of `config["sync"]["claude_dir"]` to re-expand at use site (~60 LOC across cli.py + config.py). Low risk but broad touch.
  - Depends on: Track 5A (init decomposition) landing first so `cli.py:1076` read pattern is consolidated. Otherwise the refactor fights with in-flight init work.

- **[plan-eng-review Track 1B] Rich `ConfigError` with TOML line numbers on parse failure.**
  - What: when `tomllib.load()` raises `TOMLDecodeError`, extract the line number and include in the ConfigError message.
  - Why: current error in `config.py:68-69` says "config: failed to parse /path — <raw msg>" which is serviceable but not pinpoint. A hand-edited `sync.sources` block with a syntax error should tell the user exactly which line.
  - Pros: user hand-editing config.toml gets exact line number.
  - Cons: requires tomllib's error-attribute parsing (`.line`, `.column`); low payoff since most configs are Claude Code-driven.
  - Context: relevant only for hand-edited configs.

- **[plan-eng-review 2026-04-23 Track 1A]** Full `quiet`-path audit in `cli.py`. Classify every `if not quiet:` gate as "verbose-only" vs "load-bearing signal." Track 1A patches two known load-bearing gates (`_pull_core:1445` corrupt peer manifest, `_push_core:1297` sidecar write failure). The pattern is likely wider. _src/mind_meld/cli.py, ~60 lines._ (S)

- **[plan-eng-review 2026-04-23 Track 2A]** Blob-directory as secondary peer-discovery path in corrupt-manifest recovery.
  - What: in `_collect_peer_tombstones` (or a sibling helper), when a peer's `devices/<id>.json` is corrupt or missing but `data/<id>/` has blobs and `manifests/<id>/*.enc` decrypts, promote the blob-dir-derived `device_id` to the peer list. Recover tombstones from the otherwise-dropped peer.
  - Why: codex flagged during /plan-eng-review 2026-04-23 that `list_devices()` silently dropping a peer masks a recoverable manifest. Corrupt-manifest recovery chain loses data this subtle way.
  - Pros: tightens the corruption-recovery trust surface; no observed support case today but failure mode is plausible.
  - Cons: widens the trust surface — blob-presence becomes load-bearing evidence of a peer's existence, not just a device-registry entry. ~30 LOC. Schema for blob-dir → device_id mapping needs careful docstring.
  - Observation bar: land this when we see the first real support case where corrupt `devices.json` masks a recoverable manifest. Until then, the 2A.3 shape-validation + warning is enough.
  - Depends on: Track 2A.3 landing first (structural validation in `list_devices` is a prerequisite).

- **[land-and-deploy 2026-04-23]** Add a minimal GitHub Actions CI workflow.
  - What: `.github/workflows/test.yml` that runs `pytest tests/` on every push to main and every PR. Matrix across the Python versions declared in `pyproject.toml` classifiers (3.11, 3.12). ~30 lines.
  - Why: project currently has NO CI. Pre-merge readiness for v0.8.0 depended on a human (or Claude session) running `pytest` locally. A drive-by docs/typo commit from another workspace could silently ship a broken tree to whoever installs from main. For a tool whose whole job is "never silently eat user deletions across machines," the absence of CI on main is a real risk surface — not a cosmetic one.
  - Pros: makes every future `/land-and-deploy` and `/ship` actually enforceable (not trust-based); catches drive-by breakage; matrix validates the Python-version support claim in `pyproject.toml` (3.11/3.12 currently untested).
  - Cons: first CI run surfaces latent flakes that have been hiding in the local-only workflow; need to manage any platform-specific test issues (keyring backend on Linux CI differs from macOS dev env). ~1-2 hours to get green the first time.
  - Observation: `test_crypto.py` and `test_integration.py` reach into macOS keyring indirectly via `store_passphrase_in_keyring`. Linux CI will need that stubbed or skipped via the existing `MINDMELD_PASSPHRASE` env-var path.
  - Depends on: nothing — can land anytime.

- **[land-and-deploy 2026-04-23]** (Optional) PyPI publish workflow.
  - What: `.github/workflows/release.yml` that builds + publishes to PyPI on git tag push (e.g. `v0.8.0` → trigger). Uses `hatchling` build backend (already configured in `pyproject.toml`).
  - Why: currently users install mind-meld via `pip install -e .` from a local clone. PyPI distribution would let someone `pip install mind-meld` cleanly. Only worth doing if the distribution model should shift — staying source-install is also fine for a small-user-count tool.
  - Pros: cleaner installs; discoverable via `pip search`; versioning is explicit via tags instead of "whatever's on main right now."
  - Cons: commits to a public package namespace (name squatting, can't easily rename); need to decide on trusted-publisher vs API token auth with PyPI; introduces a new release discipline (`git tag` → wait → verify on PyPI).
  - Observation bar: land this when someone asks "how do I install this" and the source-install answer becomes friction. No user demand signal today.
  - Depends on: CI workflow landing first (you want tests green before publishing anything).
