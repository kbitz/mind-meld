"""Group 8 retro-fleet skill — Claude Code consumer for the mm-events log.

Module layout:
    SKILL.md       Claude Code skill markdown (orchestrator).
    aggregator.py  Load-bearing aggregation, importable as a module.

The directory is named ``retro_fleet`` (Python identifier) on disk so it
imports cleanly and tooling (ruff / pytest) operates against it like any
other module. The symlink installer creates ``~/.claude/skills/retro-fleet``
(hyphen — Claude Code skill naming convention) pointing at this directory.
SKILL.md invokes the aggregator via the ``mm retro-fleet`` CLI subcommand
(typer wrapper at ``cli.py:retro_fleet_cmd``) so the user-facing skill name
and the Python module name can both follow their respective conventions
without collision, AND the invocation routes through the ``mm`` script
that's guaranteed to be on PATH wherever mm is installed (sidesteps the
``python`` vs ``python3`` PATH inconsistency on macOS and the pipx-venv
isolation that hides ``mind_meld`` from any other interpreter).
"""
