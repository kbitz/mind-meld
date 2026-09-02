# Track 37A — one verification command

ROADMAP.md holds the card. This file holds the review residue so the card
stays lean.

Full review (1,280 lines, 2026-09-01):
`~/.gstack/projects/kbitz-mind-meld/kbitz-fresh-workspace-verify-autoplan-plan-20260901.md`

## Approach E

One self-bootstrapping command. Cards describe verification *scope*; they
must not know where Python lives.

```
./bin/check [paths...] [-- pytest-args]
```

Default: `ruff check .` → `ruff format --check .` → `pytest tests/`.
Cheap gates first. Fail-fast. The POSIX launcher (`bin/check`) resolves
the repo root from its own location and execs a stdlib-only driver
(`bin/_check.py`). Not one bash script: `/bin/bash` is 3.2.57 on macOS,
`flock(1)` does not exist, and argparse beats hand-rolled argv.

The eight bare-`pytest` `verify:` fields existed because `AGENTS.md ##
Testing` said `Run: pytest tests/`, and `/roadmap`'s card template has no
`verify:` field. Fix the doc; the next regeneration copies it. Do not
police `ROADMAP.md`'s `verify:` strings — that is a tripwire on the
generator's normal output.

## Decomposition

`ci.yml` runs six concerns. They do not share audience, portability, or
isolation:

| Concern | Owner |
|---|---|
| ruff check / ruff format / pytest | `./bin/check` |
| Keyring backend assert | ci.yml, macOS-runner only |
| Wheel build + `mm --version` + `-m` smokes | ci.yml, fresh disposable venv, isolated HOME |
| "No module imports cli" grep | deleted — `tests/test_module_boundaries.py` is authoritative |

Local and CI share one command for the portable checks. They do not run
the same complete qualification. Do not write the false sentence.

The wheel smoke must never run in the editable environment:
`pip install --force-reinstall dist/*.whl` makes `import mind_meld`
resolve to site-packages, and `devices --format json` against a real
`~/.config/mind-meld/config.toml` reaches iCloud and the Keychain.

## Interpreter policy

- Ordered candidates: `python3.13`, `python3.12`, `python3.11`, `python3`.
- Prefer 3.13 for CI parity.
- `requires-python = ">=3.11"` has **no upper bound**. Classifiers are
  metadata. 3124/3124 tests pass on 3.14.7. NOTICE, never hard-fail, when
  the pick is not 3.13.
- Fail only when nothing >= 3.11 exists. The remedy is not
  `xcode-select --install`: `/usr/bin/python3` is 3.9.6.
- Print the resolved interpreter unconditionally.

Staleness is a content hash of `pyproject.toml` + interpreter identity +
a policy version, written to a marker after a successful install, under
the bootstrap lock. Never `pyvenv.cfg`'s mtime — that file is written
once at venv creation, so the first `pyproject.toml` edit makes every
later run reinstall forever. Hashing answers "does this venv match
declared inputs." It does not buy a lockfile's reproducibility.

Never delete a `.venv` the tool does not own. `MM_VENV` is
validate-and-use or fail, never mutate.

## `release.yml` latest-advance

Any `pyproject.toml` edit used to force-push `refs/heads/latest` to that
commit. With no version bump the tag and Release steps no-op, and
`latest` — the ref `pipx install …@latest` tracks — moved to an untagged
commit that self-reports the released version.

The guard is a shell comparison *inside* the step, evaluated *after* the
tag-creation step: `git rev-parse "$tag^{commit}"` vs `git rev-parse
HEAD`. `if: tag_exists == 'true'` inverts the logic and would skip
latest-advance on every genuine first release. Skip with `::warning::`.
`^{commit}` is required because the tag step creates lightweight tags.

Serialization of version-claiming Tracks is still right, for a different
reason: two Tracks claiming one version means the second's code never
gets tagged at all. `pyproject.toml` stays out of
`docs/shared-infra.txt`.

## `.conductor/settings.toml` precedence

Optional. `bin/check` already self-bootstraps, so this is latency
optimization.

Conductor's Mac app reads `settings.toml` from the **remote default
branch**, so the hook cannot be verified pre-merge. A
`.conductor/settings.local.toml` with a `[scripts]` table already exists
in the root checkout, and local-vs-shared `[scripts]` precedence is
documented nowhere.

Probe order, post-merge: put `setup` in `settings.local.toml` first,
confirm it fires, then move it to `settings.toml` with the local file
still present and confirm it *still* fires. Until that probe, treat the
shared `[scripts] setup` as best-effort.

The setup entry calls one repo-tracked script (`bin/conductor-setup`)
with zero inline logic, tees to a log in the workspace, and exits 0. A
hard failure would sit between the user and every new workspace for a
sub-10s convenience. Branch on `CONDUCTOR_IS_LOCAL`, not `uname`.

## Explicitly refused

- Adopting `uv` / `hatch` / `tox` under this Track (package-manager
  migration smuggled under a workspace fix). The interface holds the
  option: if the body of `bin/_check.py` changes later, every caller
  keeps working.
- A lockfile / hash-pinning of deps (real decision, real cost).
- Teaching `/roadmap` the `verify:` field (a change to a skill outside
  this repo).
- Pinning `COLUMNS` (does not fix the Rich-wrap defect; matches no
  pattern in this repo).
