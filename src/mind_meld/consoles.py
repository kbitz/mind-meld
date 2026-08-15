"""Shared Rich console singletons.

Extracted from ``cli.py`` in Track 16A. Leaf module: it imports nothing from
``mind_meld`` and must stay that way, so every module that prints can import it
without creating a cycle back through ``cli``.

**Why a shared module and not a ``Console()`` per module (load-bearing).**
``resolveflow`` and ``retention`` both print, and ``cli`` keeps printing from
the command shells and the pull/push cores. If each constructed its own
``Console``, output would still *look* right but two test seams would break:

  * ``monkeypatch.setattr(cli.console, "print", ...)`` — an INSTANCE-attribute
    patch, used by the conflict-banner tests in ``test_conflict_copy.py``. It
    only reaches ``_resolve_interactive_loop`` if that code renders through the
    same object. Pinned by ``test_module_boundaries.py``'s identity assertion.
  * width / TTY detection would be probed independently per module, so a
    non-TTY override applied to one console would not apply to the others.

Consumers use ``from mind_meld.consoles import console`` and call the bare
name. That keeps the 280-odd existing ``console.print(...)`` call sites
byte-identical through the decomposition, and a module-global rebind
(``monkeypatch.setattr(cli, "console", non_tty)``) still works for cli-resident
code, which is the only place any test does it.
"""

from __future__ import annotations

from rich.console import Console

console = Console()

# Separate stderr console so warnings/errors survive `mm ... > file` and don't
# interleave into piped stdout that another tool is parsing.
stderr_console = Console(stderr=True)

__all__ = ["console", "stderr_console"]
