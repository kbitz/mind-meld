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

from mind_meld import upgrade

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "mind_meld"
CLAUDE_MD = ROOT / "CLAUDE.md"

# `| `foo.py:bar` / `baz` / ... | doc |` — the leading cell names the owning
# file, then one or more `/`-separated symbols.
_ROW = re.compile(r"^\|(?P<cell>[^|]*)\|", re.M)
# Any `path/to/file.py:symbol` occurrence anywhere in the routing cell.
_CITATION = re.compile(r"`(?P<file>[\w/]+\.py):")
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
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # Methods count both bare and qualified: the table cites
                        # `storage/local.py:put_exclusive`, not the class.
                        names.add(sub.name)
                        names.add(f"{node.name}.{sub.name}")
                    elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                        names.add(f"{node.name}.{sub.target.id}")
                    elif isinstance(sub, ast.Assign):
                        names.update(
                            f"{node.name}.{t.id}" for t in sub.targets if isinstance(t, ast.Name)
                        )
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imported_names(path: Path) -> set[str]:
    """Names `path` merely IMPORTS. Deliberately kept apart from definitions.

    Counting these as definitions is what made the first version of this gate
    partly vacuous: `cli.py` still imports `_restore_mtime_best_effort`,
    `_stat_mtime_btime`, `_bump_canonical_mtime_post_resolve`, `CONFLICT_AGE_DAYS`,
    `console` and `safe_str`, so a routing row that kept citing `cli.py:` for
    any of them passed green even though every one of them moved.
    """
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
    return names


def _routing_citations() -> list[tuple[str, str]]:
    """(file, symbol) pairs cited in the pointer table."""
    out: list[tuple[str, str]] = []
    text = CLAUDE_MD.read_text(encoding="utf-8")
    for row in _ROW.finditer(text):
        cell = row.group("cell")
        marks = list(_CITATION.finditer(cell))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(cell)
            out.extend(_chunk_citations(m.group("file"), cell[m.end() : end]))
    return out


def _chunk_citations(fname: str, rest: str) -> list[tuple[str, str]]:
    """Split one `file.py:a / b / c` citation into (file, symbol) pairs."""
    out: list[tuple[str, str]] = []
    for chunk in rest.split("/"):
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


# ---------------------------------------------------------------------------
# The parser itself. `_imports_cli` in test_module_boundaries.py got an
# explicit non-vacuity table; `_chunk_citations` did not, and it is the more
# fragile of the two — five distinct shapes, two of which resolve against a
# DIFFERENT module than the row is keyed on.
#
# The `> 30` floors above are not a substitute: the table currently yields 97
# routing and 92 invariant citations, so a regression that silently dropped
# every dotted, wildcard and sibling-module form would still clear them by 2x
# and the gate would read as coverage while verifying a third of what it says.
# ---------------------------------------------------------------------------

_CITATION_SHAPES = [
    # Plain multi-symbol row: both resolve against the row's own file.
    (
        "cli.py",
        "`_pull_core` / `_push_core` ",
        [("cli.py", "_pull_core"), ("cli.py", "_push_core")],
    ),
    # Sibling module named explicitly — resolves against THAT module, not the row's.
    ("events.py", "`token_usage.is_cache_cold` ", [("token_usage.py", "is_cache_cold")]),
    # Dotted name whose head is NOT a module: a class attribute of the row's file.
    (
        "cli.py",
        "`PushResult.events_degradations` ",
        [("cli.py", "PushResult.events_degradations")],
    ),
    # Wildcard family — the trailing `*` must survive into the symbol.
    (
        "skill_link.py",
        "`_ensure_retro_skill_link*` ",
        [("skill_link.py", "_ensure_retro_skill_link*")],
    ),
    # Prose-only chunks are not citations, in either the parenthetical form...
    ("conflictmtime.py", "(both prompt sites share these) ", []),
    # ...or the trailing-words form.
    ("cli.py", "the `autopush` breadcrumb outcome ", []),
    # A chunk that is a bare filename is skipped, not mistaken for a symbol.
    ("cli.py", "`pullhistory.py` ", []),
]


@pytest.mark.parametrize("fname,rest,expected", _CITATION_SHAPES)
def test_citation_parser_is_not_vacuous(fname: str, rest: str, expected: list) -> None:
    """Every shape the two routing tables actually use still parses."""
    assert _chunk_citations(fname, rest) == expected


# The one routing row whose symbol the parser cannot see: prose shares the
# `/`-chunk with the symbol, so the whole chunk is discarded as a prose tail
# and `cli.py:_download_and_apply` is verified by NOTHING. It happens to be
# correct today. It would not be caught if the function moved -- which is the
# single failure mode this file exists to prevent, and `_download_and_apply` is
# a plausible candidate for a later extraction Track.
#
# Fix is one edit to CLAUDE.md: put the prose in its own `/`-chunk, i.e.
# ``cli.py:_download_and_apply` / (rel_path + base_path concatenation site)`.
# This list must shrink to [], never grow.
# Empty as of v0.12.21: the one offending row was rewritten to
# "`cli.py:_download_and_apply` / (rel_path + base_path concatenation site)",
# which parses. Keep it at [] -- an entry here is a row nothing verifies.
_ROWS_WITH_UNPARSEABLE_CITATIONS: list[str] = []


def test_every_routing_row_resolves_at_least_one_symbol() -> None:
    """A row that cites a `.py` file must yield a citation the gate can check.

    Without this, a row written in the "symbol plus prose in one chunk" shape
    is silently unverified: `test_routing_citation_resolves` is parametrized
    over what the parser FOUND, so a row it found nothing in simply generates
    no test case and the suite still goes green.
    """
    text = CLAUDE_MD.read_text(encoding="utf-8")
    unparsed = []
    for row in _ROW.finditer(text):
        cell = row.group("cell")
        marks = list(_CITATION.finditer(cell))
        if not marks:
            continue
        got = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(cell)
            got.extend(_chunk_citations(m.group("file"), cell[m.end() : end]))
        if not got:
            unparsed.append(cell.strip())

    unexpected = [
        c for c in unparsed if not any(known in c for known in _ROWS_WITH_UNPARSEABLE_CITATIONS)
    ]
    assert unexpected == [], (
        f"routing rows cite a .py file but resolve no symbol: {unexpected}. "
        f"Put prose in its own `/`-chunk so the symbol stands alone."
    )
    assert len(unparsed) == len(_ROWS_WITH_UNPARSEABLE_CITATIONS), (
        "a known-unparseable row was fixed — delete it from "
        "_ROWS_WITH_UNPARSEABLE_CITATIONS so the list keeps shrinking"
    )


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
    if symbol in _defined_names(path):
        return
    hint = (
        " — it is only IMPORTED there, so it lives somewhere else now"
        if symbol in _imported_names(path)
        else ""
    )
    raise AssertionError(
        f"CLAUDE.md routes {fname}:{symbol}, which is not defined there{hint}. "
        f"The symbol moved, or the row is stale."
    )


def test_every_changelog_version_has_a_progress_row() -> None:
    """Every released version appears in docs/PROGRESS.md.

    Fourth occurrence of this gap: v0.11.24 and v0.11.27 are already named in
    CLAUDE.md, and v0.12.12 and v0.12.18 were found missing during Track 16A.
    The v0.11.24 design tried to auto-append the row from `release.yml` via
    `git push`, which branch protection rejects on every release where the row
    was not already in the PR -- so the workflow is not the place to fix this.

    A test is: the row goes in the SAME PR as the pyproject + CHANGELOG bump,
    and CI fails the PR if it does not. That is the durable fix CLAUDE.md's
    PROGRESS-row convention asks for, and it costs nothing at release time.
    """
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    progress = (ROOT / "docs" / "PROGRESS.md").read_text(encoding="utf-8")

    released = re.findall(r"^## \[(\d+\.\d+\.\d+(?:\.\d+)?)\]", changelog, re.M)
    assert len(released) > 20, "CHANGELOG parse found suspiciously few releases"

    charted = set(re.findall(r"^\|\s*(\d+\.\d+\.\d+(?:\.\d+)?)\s*\|", progress, re.M))

    # Enforced from 0.11.0 forward. Running this gate for the first time turned
    # up nine OLDER gaps too -- 0.10.3, 0.10.2, 0.10.0, 0.9.6, 0.9.5, 0.8.8,
    # 0.8.7, 0.8.6, 0.1.0 -- so the problem is considerably older than the two
    # occurrences CLAUDE.md names. They are listed here rather than silently
    # excluded: backfilling pre-0.11 prose is a separate call, and the point of
    # this gate is to stop the RECURRENCE, not to relitigate history.
    def _key(v: str) -> tuple[int, ...]:
        return tuple(int(part) for part in v.split("."))

    baseline = (0, 11, 0)
    missing = [v for v in released if v not in charted and _key(v) >= baseline]
    assert missing == [], (
        f"CHANGELOG has {missing} with no docs/PROGRESS.md row. Add the row in "
        f"the same PR as the version bump — release.yml cannot push it to a "
        f"protected branch, which is why the v0.11.24 auto-append design failed."
    )


def _string_constants(node: ast.AST) -> list[str]:
    """Literal string pieces inside a call argument (incl. f-string constants)."""
    out: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.append(node.value)
    elif isinstance(node, ast.JoinedStr):
        for part in node.values:
            out.extend(_string_constants(part))
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        out.extend(_string_constants(node.left))
        out.extend(_string_constants(node.right))
    return out


def test_skill_md_step0_preflight_contract() -> None:
    """Step 0 is an unskippable binary gate above Step 1.

    ``split(heading, 1)[-1]`` returns the whole document when the heading is
    absent, and ``command -v mm`` already appears in Step 1 historically, so
    that idiom would pass for a *missing* Step 0. Assert both headings exist,
    then slice by index. Do not reuse the Notes-decoder test's boundaries —
    that couples two independent contracts to one pair of headings.
    """
    skill = (ROOT / "src" / "mind_meld" / "skills" / "retro_fleet" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "## Step 0" in skill, "Step 0 preflight heading is gone"
    assert "## Step 1" in skill
    i0, i1 = skill.index("## Step 0"), skill.index("## Step 1")
    assert i0 < i1, "Step 0 must precede Step 1"
    step0 = skill[i0:i1]
    # Positive substrings alone do not protect a gate: "never run `command -v
    # mm`" would satisfy them. Assert the STOP semantics too, and assert the
    # Step 1 clause POSITIVELY -- forbidding one phrase passes if the clause is
    # deleted or rewritten unconditionally.
    assert "command -v mm" in step0
    assert "mm --version" in step0, "0A must probe the binary, not just resolve it"
    assert step0.count("STOP") >= 2, "both 0A branches must stop the run"
    assert "Do not run Steps 1-5" in step0
    assert "Skip this step" not in step0, "Step 0 must carry no escape hatch"
    # D2 cut the version-comparison stage: two matching stale numbers read as
    # verification, and the check cannot know which SKILL.md the agent loaded.
    assert "min_mm_version" not in step0
    assert "skill_version" not in step0
    assert "sort -V" not in step0

    # Prose wraps. Assert against whitespace-normalized text so reflowing a
    # paragraph is not a test failure -- the contract is the sentence, not
    # where the line breaks fall.
    flat = " ".join(step0.split())
    assert "restart the agent so it reloads SKILL.md" in flat

    # Stage 0B relays mm's own upgrade nudge, which is the only
    # network-authoritative staleness signal the skill can reach. Bind the
    # command to the constant rather than a copy: this Track removed two
    # rotting version literals, so it must not add a rotting command literal.
    # Asserting the constant also pins 0B's existence -- without it, deleting
    # the whole relay leaves this test green.
    assert upgrade.INSTALL_CMD in flat, "Stage 0B must quote upgrade.INSTALL_CMD verbatim"
    step1 = skill[i1 : skill.index("## Step 2")]
    assert "Skip this step" not in step1, "Step 1's skip clause must name Step 1"
    assert "Skip Step 1 only if" in step1, (
        "Step 1's skip clause must still exist and stay conditional -- deleting "
        "it, or making it unconditional, silently re-opens the Step 0 bypass"
    )


def test_every_notes_line_has_a_skill_decoder_entry() -> None:
    """Every aggregator Notes line has a SKILL.md decoder entry.

    SKILL.md's Notes section is the AI agent's API doc. A new Notes line the
    decoder doesn't cover means the agent either drops it or invents an
    explanation. Retro-catches the v0.12.37 drift where aggregator emitted
    ``pre-v0.11.14 OR cold token cache`` while SKILL.md documented
    ``pre-v0.11.0 session schema and/or pre-v0.11.14``.
    """
    aggregator = (
        ROOT / "src" / "mind_meld" / "skills" / "retro_fleet" / "aggregator.py"
    ).read_text(encoding="utf-8")
    skill = (ROOT / "src" / "mind_meld" / "skills" / "retro_fleet" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    decoder = skill.split("## Notes section in aggregator output", 1)[-1].split(
        "## Trends vs prior", 1
    )[0]

    required: list[str] = []
    tree = ast.parse(aggregator)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "append":
            continue
        if not isinstance(func.value, ast.Name):
            continue
        # notes.append(...) and coverage_reasons.append(...) both feed Notes.
        if func.value.id not in {"notes", "coverage_reasons"}:
            continue
        if not node.args:
            continue
        pieces = [" ".join(c.split()) for c in _string_constants(node.args[0])]
        identifying = next((p for p in pieces if len(p) >= 16), None)
        if identifying is not None:
            required.append(identifying[:32].rstrip())

    assert required, "parser found no Notes stems — the extractor broke"
    missing = [p for p in required if p not in decoder]
    assert missing == [], (
        "aggregator Notes identifying fragments with no SKILL.md decoder "
        "entry: " + "; ".join(missing)
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
        "host_skill_discovery.py",
    ):
        assert f"`{mod}:" in table, f"{mod} has no routing row — Group 17 would fly blind"


# ---------------------------------------------------------------------------
# The invariant docs carry their OWN routing header, and it drifted where
# CLAUDE.md's table did not.
# ---------------------------------------------------------------------------

_INVARIANT_ROW = re.compile(r"^- `src/mind_meld/(?P<file>[\w/]+\.py)` — (?P<rest>.+)$", re.M)


def _invariant_citations() -> list[tuple[str, str, str]]:
    """(doc, file, symbol) for every `- \\`src/mind_meld/x.py\\` — \\`a\\` / \\`b\\`` line."""
    out: list[tuple[str, str, str]] = []
    for doc in sorted((ROOT / "docs" / "invariants").glob("*.md")):
        for m in _INVARIANT_ROW.finditer(doc.read_text(encoding="utf-8")):
            for fname, sym in _chunk_citations(m.group("file"), m.group("rest")):
                out.append((doc.name, fname, sym))
    return out


def test_invariant_docs_have_citations() -> None:
    assert len(_invariant_citations()) > 30


@pytest.mark.parametrize("doc,fname,symbol", _invariant_citations())
def test_invariant_doc_citation_resolves(doc: str, fname: str, symbol: str) -> None:
    """Each invariant doc's own "Read BEFORE editing" list must resolve too.

    CLAUDE.md's table routes an agent to the right DOC; that doc's first lines
    then route them to the right CODE. Track 16A re-anchored the table but not
    these, so `conflicts.md` still filed `_resolve_interactive_loop` and
    `_find_conflict_files` under `cli.py`, and `events-retro.md` filed seven
    moved symbols there plus `_devices_json_cmd`, which never existed. The
    gate that only read CLAUDE.md could not see any of it — an agent would be
    routed correctly and then sent straight back to the wrong file.
    """
    candidates = [SRC / fname, SRC / "skills" / "retro_fleet" / Path(fname).name]
    path = next((c for c in candidates if c.exists()), None)
    assert path is not None, f"{doc} routes to src/mind_meld/{fname}, which does not exist"

    if symbol.endswith("*"):
        stem = symbol.rstrip("*")
        assert any(n.startswith(stem) for n in _defined_names(path)), (
            f"{doc} routes {fname}:{symbol} but nothing there starts with {stem!r}"
        )
        return
    if symbol in _defined_names(path):
        return
    hint = (
        " — it is only IMPORTED there, so it lives somewhere else now"
        if symbol in _imported_names(path)
        else ""
    )
    raise AssertionError(
        f"{doc} routes {fname}:{symbol}, which is not defined there{hint}. "
        f"The symbol moved, or the row is stale."
    )


_DIAG_JSON_TOP_LEVEL = (
    "mm_version",
    "config",
    "crypto_init",
    "root_salt_drift",
    "sidecar",
    "storage_inventory",
    "last_autorun",
    "skill_links",
    "host_skill_discovery",
    "discovery",
)
_HOST_SKILL_DISCOVERY_FIELDS = (
    "host",
    "status",
    "claude_skills_compat",
    "retro_fleet_resolved",
    "retro_fleet_path",
    "grok_version",
)
_SKILL_LINKS_ROW_FIELDS = (
    "key",
    "agent",
    "target",
    "store",
    "store_state",
    "status",
    "maintain_links",
)


def _collect_diag_state_keys() -> set[str]:
    tree = ast.parse((SRC / "cli.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_collect_diag_state":
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict):
                    keys = {
                        k.value
                        for k in child.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
                    if keys:
                        return keys
    raise AssertionError("_collect_diag_state return dict not found")


def test_readme_diag_json_fields_match_emitted_keys() -> None:
    """README's documented `mm diag --json` field list must match the code.

    Nothing enforced this, so README.md's troubleshooting entry could name
    fields `_collect_diag_state` no longer emits, or omit a new sibling key
    like `host_skill_discovery`.
    """
    emitted = _collect_diag_state_keys()
    assert emitted == set(_DIAG_JSON_TOP_LEVEL), (
        f"_collect_diag_state keys {sorted(emitted)} != documented {list(_DIAG_JSON_TOP_LEVEL)}"
    )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "`mm diag --json`" in readme
    missing_top = [k for k in _DIAG_JSON_TOP_LEVEL if f"`{k}`" not in readme]
    assert missing_top == [], f"README does not name mm diag --json top-level keys {missing_top}"
    missing_hsd = [k for k in _HOST_SKILL_DISCOVERY_FIELDS if f"`{k}`" not in readme]
    assert missing_hsd == [], f"README does not name host_skill_discovery fields {missing_hsd}"
    missing_rows = [k for k in _SKILL_LINKS_ROW_FIELDS if f"`{k}`" not in readme]
    assert missing_rows == [], f"README does not name skill_links row fields {missing_rows}"
