"""Tests for the version source-of-truth migration.

`src/mind_meld/__init__.py` reads the version via `importlib.metadata` with a
`"0.0.0+dev"` fallback for uninstalled source-tree runs. `mm --version` is
wired to the same value.
"""

from __future__ import annotations

import importlib
import sys
from unittest import mock


def test_version_installed_package_returns_pyproject_version():
    """In the test env the package IS installed (editable); __version__ must
    match the pyproject version."""
    import mind_meld

    assert mind_meld.__version__ != "0.0.0+dev"
    # pyproject.toml is the single source of truth
    import tomllib
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    with open(repo_root / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)
    assert mind_meld.__version__ == pyproject["project"]["version"]


def test_version_missing_package_returns_dev_sentinel():
    """When importlib.metadata raises PackageNotFoundError (fresh clone
    without `pip install -e .`), fall back to '0.0.0+dev' so imports
    don't crash and dev state is obvious in bug reports."""
    from importlib.metadata import PackageNotFoundError

    def _raise_not_found(_name: str) -> str:
        raise PackageNotFoundError("mind-meld")

    with mock.patch("importlib.metadata.version", _raise_not_found):
        # Drop the cached module and re-import so the top-level try/except
        # in __init__.py actually runs against the patched version().
        sys.modules.pop("mind_meld", None)
        try:
            reloaded = importlib.import_module("mind_meld")
            assert reloaded.__version__ == "0.0.0+dev"
        finally:
            sys.modules.pop("mind_meld", None)
            importlib.import_module("mind_meld")  # restore real module


def test_mm_version_cli_flag(tmp_path, monkeypatch):
    """`mm --version` prints the version and exits 0."""
    from typer.testing import CliRunner

    from mind_meld.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "mm " in result.stdout


def test_no_source_file_reads_a_version_file():
    """Regression guard: the VERSION file was deleted. No source file may
    read it (grep-style sweep). Catches a future contributor re-adding a
    `open('VERSION')` or `Path('VERSION').read_text()` pattern."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src"

    forbidden_patterns = [
        'open("VERSION")',
        "open('VERSION')",
        'Path("VERSION")',
        "Path('VERSION')",
        'read_text("VERSION")',
    ]

    for py_file in src_dir.rglob("*.py"):
        text = py_file.read_text()
        for pat in forbidden_patterns:
            assert pat not in text, (
                f"{py_file} reads a VERSION file ({pat!r}); the file was "
                f"deleted. Use `mind_meld.__version__` instead."
            )
