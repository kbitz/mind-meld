"""Wheel-shape pins for the skills/ subpackage.

Group 7 preflight ships a placeholder src/mind_meld/skills/ package
(__init__.py + .gitkeep) so the wheel always contains the skills/
subpackage via hatchling's `packages = ["src/mind_meld"]`. Group 8
fills it with retro-fleet/SKILL.md; the assertions here pin the
contract that future build-backend changes (zipped wheels, dropped
subpackage, etc.) cannot silently strip the skills/ package without CI
noticing.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path


def test_skills_package_importable():
    import mind_meld.skills  # noqa: F401  (import-as-assertion)


def test_skills_resources_dir_exists():
    skills_dir = importlib.resources.files("mind_meld") / "skills"
    assert skills_dir.is_dir(), (
        "src/mind_meld/skills/ must ship inside the installed package. "
        'It rides on hatchling\'s `packages = ["src/mind_meld"]` via '
        "the __init__.py making it a real subpackage. If this fails, "
        "check pyproject.toml hasn't excluded the dir or moved to a "
        "build backend that ignores subpackages without explicit listing."
    )


def test_skills_dir_is_real_path():
    """Sanity check: hatchling default = unzipped wheels.

    Group 8's symlink installer (`_ensure_retro_skill_link`) uses
    `Path(str(importlib.resources.files(...)))` and then `.symlink_to`,
    which only works against a real filesystem path (not a zip resource).
    If a future build backend ships zipped wheels, this fails BEFORE
    Group 8's installer would silently no-op for users.
    """
    skills_dir = Path(str(importlib.resources.files("mind_meld") / "skills"))
    assert skills_dir.exists()
    assert skills_dir.is_dir()
