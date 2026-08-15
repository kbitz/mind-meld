"""CLAUDE.md's invariant pointer table must route to code that exists.

The table is the first thing an agent reads before editing a load-bearing
surface. Before Track 16A it was keyed entirely on ``cli.py:<function>``, so the
decomposition would have left five of nine rows pointing at symbols that no
longer live in that file — and, worse, an agent opening ``resolveflow.py`` would
have grepped the table, got ZERO hits, read no invariant doc, and edited the
conflict dispatch blind. That is the exact population Group 17 is: five agents,
each opening one of the new modules.

`docs/ROADMAP.md` Track 18A notes every pinned LINE number in the invariant docs
has already drifted at least once. This is the durable answer: cite symbols, and
make CI prove the citations resolve.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "mind_meld"
CLAUDE_MD = ROOT / "CLAUDE.md"

# `| `foo.py:bar` / `baz` / ... | doc |` — the leading cell names the owning
# file, then one or more `/`-separated symbols.
_ROW = re.compile(r"^\|\s*`(?P<file>[\w/]+\.py):(?P<rest>.+?)`?\s*\|", re.M)
_SYMBOL = re.compile(r"`?([A-Za-z_][\w.]*\*?)`?")


def _defined_names(path: Path) -> set[str]:
    """Every top-level def/class/assignment in `path`, plus its dotted attrs."""
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                        names.add(f"{node.name}.{sub.target.id}")
                    elif isinstance(sub, ast.Assign):
                        names.update(
                            f"{node.name}.{t.id}" for t in sub.targets if isinstance(t, ast.Name)
                        )
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
    return names


def _routing_citations() -> list[tuple[str, str]]:
    """(file, symbol) pairs cited in the pointer table."""
    out: list[tuple[str, str]] = []
    for m in _ROW.finditer(CLAUDE_MD.read_text(encoding="utf-8")):
        fname = m.group("file")
        for chunk in m.group("rest").split("/"):
            chunk = chunk.strip()
            # Prose tails like "(both prompt sites share these)" or
            # "rel_path / base_path concatenation site" are not citations.
            if not chunk or chunk.startswith("(") or " " in chunk.strip("`"):
                continue
            sym = _SYMBOL.match(chunk)
            if not sym or sym.group(1).endswith(".py"):
                continue
            name = sym.group(1)
            # A row keyed on one file may cite a sibling module explicitly as
            # `other_module.symbol` (e.g. the tolerant-binary-reads row lists
            # token_usage and pullhistory readers alongside events' own).
            # Resolve those against the named module, not the row's file.
            if "." in name:
                owner, _, leaf = name.rpartition(".")
                if (SRC / f"{owner}.py").exists():
                    out.append((f"{owner}.py", leaf))
                else:
                    # A dotted name whose head is not a module is an attribute
                    # of a class in THIS row's file, e.g. `PushResult.events_
                    # degradations`. _defined_names() records those dotted.
                    out.append((fname, name))
            else:
                out.append((fname, name))
    return out


def test_routing_table_has_citations() -> None:
    """Guard against the parser silently matching nothing."""
    assert len(_routing_citations()) > 30


@pytest.mark.parametrize("fname,symbol", _routing_citations())
def test_routing_citation_resolves(fname: str, symbol: str) -> None:
    """Every `<file>.py:<symbol>` in the table names a real definition."""
    candidates = [SRC / fname, SRC / "skills" / "retro_fleet" / Path(fname).name]
    path = next((c for c in candidates if c.exists()), None)
    assert path is not None, f"CLAUDE.md routes to {fname}, which does not exist"

    if symbol.endswith("*"):  # wildcard family, e.g. `_ensure_retro_skill_link*`
        stem = symbol.rstrip("*")
        assert any(n.startswith(stem) for n in _defined_names(path)), (
            f"CLAUDE.md routes {fname}:{symbol} but nothing there starts with {stem!r}"
        )
        return
    assert symbol in _defined_names(path), (
        f"CLAUDE.md routes {fname}:{symbol}, which is not defined there — "
        f"the symbol moved, or the row is stale"
    )


def test_every_extracted_module_has_a_routing_row() -> None:
    """The Track 16A modules each get at least one row.

    This is the failure the decomposition would otherwise have shipped: the
    table keyed on source file, so the new modules returned empty for the very
    agents about to edit them.
    """
    table = CLAUDE_MD.read_text(encoding="utf-8")
    for mod in (
        "resolveflow.py",
        "events_tail.py",
        "skill_link.py",
        "retention.py",
        "conflictmtime.py",
    ):
        assert f"`{mod}:" in table, f"{mod} has no routing row — Group 17 would fly blind"
