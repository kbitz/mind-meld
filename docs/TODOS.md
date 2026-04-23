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
