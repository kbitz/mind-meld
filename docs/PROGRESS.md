# Progress

## Version history

| Version | Released | Headline |
|---------|----------|----------|
| 0.4.0   | 2026-04-21 | Conflict-copy preservation + mtime-skip for pull |
| 0.3.0   | (pre-v0.4) | Additive-only sync model with tombstones and conflict resolution |
| 0.2.0   | (pre-v0.3) | Multi-source sync with gstack support |
| 0.1.x   | (pre-v0.2) | iCloud-only backend, autopull/autopush for Claude Code; scoped sync to memory/todos |

## Current phase: pre-1.0 cleanup sweep (v0.x → v1.0)

All work is driving toward a single v1.0 release that cleans up correctness
bugs, decomposition debt, and multi-source assumption lag from the five prior
PRs, then layers on three pre-1.0 features (selective sync, mtime hash cache,
three-way merge base).

See `docs/ROADMAP.md` for the structured Groups > Tracks > Tasks plan.

### Where we are

- **Group 1 (Correctness foundation):** not started — blocks everything.
- **Groups 2–6 (Refactor + hygiene):** not started — queued after Group 1.
- **Groups 7–9 (P2/P3 features, parallel-safe after Group 6):** not started.

### Known version-source drift

The repository currently has three inconsistent version sources — `VERSION`
says 0.4.0, `pyproject.toml` says 0.3.0, and `src/mind_meld/__init__.py` says
0.2.0. `init` prints `__version__` so users see 0.2.0 despite a 0.4.0 release.
Group 1 pre-flight resolves this.
