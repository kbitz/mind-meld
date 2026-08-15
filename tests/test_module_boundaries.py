"""Structural gates for the Track 16A decomposition of cli.py.

These are the checks that actually gate the move. The Track's original plan
proposed byte-equality of moved function text as the gate; byte-equality proves
textual provenance and nothing else. It cannot detect a free name resolving to
a different module global, a missing supporting function, an import cycle, a
dead monkeypatch, or a changed singleton identity. Every test here catches one
of those.

Read ``docs/invariants/conflicts.md`` and ``docs/invariants/events-retro.md``
before relaxing anything in this file.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "mind_meld"

# The modules extracted out of cli.py, plus the two leaves they depend on.
# Adding a module here is how a future extraction opts into every gate below.
EXTRACTED = [
    "consoles",
    "conflictmtime",
    "skill_link",
    "events_tail",
]

LEAVES = ["consoles", "conflictmtime", "safety", "conflictdiff", "fsutil"]


# ---------------------------------------------------------------------------
# T1 — standalone import, in every order.
#
# A partially-initialized-module cycle only manifests in ONE import order, so
# importing the package under test in-process proves nothing: pytest has
# already imported half of it. Each case runs in a FRESH subprocess.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mod", [*EXTRACTED, "cli"])
def test_module_imports_standalone(mod: str) -> None:
    """Each module imports on its own, with nothing else from the package loaded.

    Regression pin for the cycle Track 16A would otherwise have shipped:
    `resolveflow` and `retention` both need `console`, which used to be defined
    at cli.py:277. With cli importing them at module scope, the import lands
    mid-execution of cli and raises
    `ImportError: cannot import name 'console' from partially initialized
    module`. Every `mm` invocation dies. CI's `mm --version` smoke catches it
    too, but only after the whole extraction is written.
    """
    r = subprocess.run(
        [sys.executable, "-c", f"import mind_meld.{mod}"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"import mind_meld.{mod} failed:\n{r.stderr}"


@pytest.mark.parametrize("first", EXTRACTED)
def test_import_order_does_not_matter(first: str) -> None:
    """Importing an extracted module BEFORE cli works, and vice versa.

    Import order is the discriminator between "acyclic" and "happens to work
    because cli always won the race".
    """
    for order in ([first, "cli"], ["cli", first]):
        stmt = "; ".join(f"import mind_meld.{m}" for m in order)
        r = subprocess.run([sys.executable, "-c", stmt], capture_output=True, text=True)
        assert r.returncode == 0, f"order {order} failed:\n{r.stderr}"


# ---------------------------------------------------------------------------
# T2 — no extracted module may import cli, at module scope OR function scope.
# ---------------------------------------------------------------------------


def _imports_cli(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, source-ish) for every import of mind_meld.cli in `path`.

    Walks the whole tree, not just module-level body, because a function-local
    `from mind_meld import cli` is exactly how a cycle gets papered over — and
    ruff's F811 cannot see function-local shadowing, so lint will never catch
    it. `docs/ROADMAP.md` Track 17E exists to delete thirteen such re-imports;
    this Track must not add more.
    """
    hits: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "mind_meld.cli" or a.name.startswith("mind_meld.cli."):
                    hits.append((node.lineno, f"import {a.name}"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "mind_meld.cli" or mod.startswith("mind_meld.cli."):
                hits.append((node.lineno, f"from {mod} import ..."))
            elif mod == "mind_meld" and any(a.name == "cli" for a in node.names):
                hits.append((node.lineno, "from mind_meld import cli"))
    return hits


@pytest.mark.parametrize("mod", EXTRACTED)
def test_extracted_module_never_imports_cli(mod: str) -> None:
    hits = _imports_cli(SRC / f"{mod}.py")
    assert hits == [], f"{mod}.py imports cli at {hits} — that is the cycle, not a workaround"


@pytest.mark.parametrize("mod", LEAVES)
def test_leaf_modules_import_nothing_from_cli(mod: str) -> None:
    """Leaves are the cycle-break. If one grows a cli import the break is gone."""
    hits = _imports_cli(SRC / f"{mod}.py")
    assert hits == [], f"leaf {mod}.py must not import cli (found {hits})"


def test_no_module_under_src_imports_cli() -> None:
    """Repo-wide: nothing in the package imports cli.

    `aggregator.py` reaches the CLI as a SUBPROCESS
    (`sys.executable -m mind_meld.cli devices --format json`), never as an
    import, and that is the only supported direction. An import would also
    break that subprocess: under `-m`, cli.py executes as `__main__` and
    `sys.modules["mind_meld.cli"]` is unpopulated, so a function-local
    `from mind_meld import cli` re-executes cli.py top to bottom — a second
    Typer app and a second Console whose stray output would corrupt the JSON
    the aggregator parses.
    """
    offenders = {
        str(p.relative_to(SRC)): hits
        for p in SRC.rglob("*.py")
        if p.name != "cli.py" and (hits := _imports_cli(p))
    }
    assert offenders == {}, f"modules importing cli: {offenders}"


# ---------------------------------------------------------------------------
# T5 — console singleton identity.
# ---------------------------------------------------------------------------


def test_console_singletons_are_shared() -> None:
    """Every consumer renders through the SAME Console objects.

    Four tests in test_conflict_copy.py patch `cli.console.print` — an INSTANCE
    attribute — and then drive the conflict prompt. If a module constructed its
    own Console, those patches would capture nothing and the tests would fail
    confusingly (empty output, looks like a rendering regression).
    """
    from mind_meld import cli, consoles

    assert cli.console is consoles.console
    assert cli.stderr_console is consoles.stderr_console


def test_only_consoles_module_constructs_a_console() -> None:
    """`Console()` is constructed in exactly one place in the package."""
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if p.name != "consoles.py" and "Console(" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"construct consoles in consoles.py only; found in {offenders}"


# ---------------------------------------------------------------------------
# T6 — both sides of the mtime leaf resolve to the same functions.
# ---------------------------------------------------------------------------


def test_mtime_primitives_are_shared_not_copied() -> None:
    """cli and the conflict resolver use one implementation, not two.

    `docs/invariants/conflicts.md` makes the post-(l)ocal canonical bump
    load-bearing: without it the user loops resolve -> pull -> resolve forever.
    The Track 16A cut puts the bump's two callers (`cli._drain_inline_bumps`
    and the resolver's (l)ocal / (p)romote branches) in different modules, so
    pin that they still share one function object.
    """
    from mind_meld import cli, conflictmtime

    assert cli._stat_mtime_btime is conflictmtime._stat_mtime_btime
    assert cli._bump_canonical_mtime_post_resolve is (
        conflictmtime._bump_canonical_mtime_post_resolve
    )
    assert cli._restore_mtime_best_effort is conflictmtime._restore_mtime_best_effort


def test_future_clamp_constant_has_one_owner() -> None:
    """The future-clamp skew is defined once, in the leaf.

    Both `_restore_mtime_best_effort` (pull side) and
    `_bump_canonical_mtime_post_resolve` (resolve side) clamp against it, and
    `docs/invariants/sync.md` calls the symmetry load-bearing — a copied
    constant is free to drift.
    """
    from mind_meld import conflictmtime

    assert conflictmtime._MTIME_RESTORE_MAX_SKEW_SECONDS == 60.0
    others = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if p.name != "conflictmtime.py"
        and "_MTIME_RESTORE_MAX_SKEW_SECONDS = " in p.read_text(encoding="utf-8")
    ]
    assert others == [], f"constant redefined in {others}"


# ---------------------------------------------------------------------------
# T8 — the test suite must never reach the real skill installer.
# ---------------------------------------------------------------------------


def test_real_home_guard_fires(tmp_path: Path) -> None:
    """The installer refuses to touch the developer's real HOME under pytest.

    Non-vacuousness matters here: when this guard was added, **67 tests** were
    reaching the real ``~/.claude/skills`` / ``~/.codex/skills`` /
    ``~/.config/opencode/skills``. They all pass now because conftest's
    ``_isolate_skill_links`` redirects the roots. If that fixture ever stops
    working, this guard is what turns a silent mutation of the developer's
    machine back into a failing test.
    """
    from mind_meld import skill_link

    with pytest.raises(AssertionError, match="real skill installer"):
        skill_link._refuse_real_home_under_pytest(Path("~/.claude/skills/retro-fleet").expanduser())

    # A redirected target is fine -- the guard is about location, not shape.
    skill_link._refuse_real_home_under_pytest(tmp_path / "agents" / "claude" / "retro-fleet")


def test_skill_roots_are_redirected_for_this_test() -> None:
    """conftest's autouse fixture is actually in effect (not silently skipped)."""
    from mind_meld import skill_link

    assert all(not r.startswith("~") for r in skill_link.SKILL_ROOTS), skill_link.SKILL_ROOTS


# ---------------------------------------------------------------------------
# T11 — the shipped entrypoints, as subprocesses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["--version"], ["--help"]])
def test_dash_m_entrypoint(argv: list[str]) -> None:
    """`python -m mind_meld.cli` is what the retro-fleet skill actually invokes.

    CI only ever smoked the `mm` console-script path, so the `-m` path — the one
    `aggregator.get_known_devices()` shells out to and JSON-parses — has never
    been covered. It degrades to `(None, [])` on ANY failure, so a break here is
    silent: every `mm retro-fleet` card would just quietly drop its
    "of M devices" tail.
    """
    r = subprocess.run(
        [sys.executable, "-m", "mind_meld.cli", *argv],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


def test_command_set_is_unchanged_by_the_decomposition() -> None:
    """Every @app.command() shell stays registered in cli.py.

    Name-set equality is weaker than dispatch (it misses broken callbacks), but
    it is the cheap pin that catches a command whose decorator moved out with
    its implementation. Dispatch itself is covered by the per-command tests.
    """
    from mind_meld.cli import app

    names = {c.name or c.callback.__name__.rstrip("_") for c in app.registered_commands}
    assert names == {
        "autopull",
        "autopush",
        "conflicts",
        "devices",
        "diag",
        "diff",
        "disable-source",
        "enable-source",
        "gc",
        "init",
        "install-skills",
        "log",
        "migrate-config",
        "pull",
        "push",
        "reconfigure-sources",
        "recover",
        "refresh-identity",
        "resolve",
        "retro-fleet",
        "sources",
        "status",
    }
