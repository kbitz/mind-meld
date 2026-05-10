# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

## Unprocessed

- [eng-review] **gc_cache_entries: validate max_age_s=0 / fractional-day semantics** — `int(max_age_s / 86400)` silently floors fractional days; `max_age_s=0` effectively reaps every entry. Pre-existing behavior, never tested. Add explicit tests for `max_age_s=0` (reap-all) and `max_age_s=43200` (12 hours; floors to 0 days = reap-all? or document as not-recommended). Either pin current behavior with tests or guard the input. _src/mind_meld/token_usage.py:938-947, ~10 LOC test._ Surfaced by Codex outside-voice on Track 11A review (2026-05-10). Depends on: Track 11A merging.
- [eng-review] **Add direct cache-isolation positive test** — autouse `_isolate_token_cache` fixture relies on monkeypatch at runtime, but there's no positive test asserting no real `~/.config/mind-meld/session-tokens.json` was touched during a pytest run. Stat-snapshot the real path before suite start; assert mtime unchanged after suite end (or use a `HOME`/`XDG_CONFIG_HOME` trap). Stronger regression pin than the chosen "pytest twice output identical" pre-flight. _tests/conftest.py + new tests/test_isolation_guard.py, ~15 LOC._ Surfaced by Codex outside-voice on Track 11A review (2026-05-10). Depends on: Track 11A merging.
