"""Mind Meld — sync Claude Code sessions and AI developer context across machines."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mind-meld")
except PackageNotFoundError:
    # Running from a source tree that has not been installed (e.g. fresh clone
    # without `pip install -e .`). Use a sentinel that makes the dev state
    # obvious in error reports.
    __version__ = "0.0.0+dev"
