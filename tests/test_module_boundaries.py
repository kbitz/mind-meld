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

HOST_ADAPTER_MODULES = [
    f"hosts.{p.stem}" for p in sorted((SRC / "hosts").glob("*.py")) if p.name != "__init__.py"
]

# The modules extracted out of cli.py / host_usage.py, plus the leaves they depend on.
# Adding a module here is how a future extraction opts into every gate below.
EXTRACTED = [
    "attemptlog",
    "pullplan",
    "consoles",
    "conflictmtime",
    "skill_link",
    "events_tail",
    "resolveflow",
    "retention",
    "core",
    "statusflow",
    "host_registry",
    *HOST_ADAPTER_MODULES,
]

LEAVES = [
    "statedb",
    "attemptlog",
    "pullplan",
    "consoles",
    "conflictmtime",
    "safety",
    "conflictdiff",
    "fsutil",
    "gitenv",
    "updateprogress",
]


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
            if node.level:
                # RELATIVE import. Every file here lives directly in
                # src/mind_meld/, so `from .cli import x` has module == "cli"
                # and `from . import cli` has module == None with "cli" in
                # names. Both re-create the exact cycle, and both were
                # invisible before this branch — node.level was never read.
                if mod == "cli" or mod.startswith("cli."):
                    hits.append((node.lineno, f"from {'.' * node.level}{mod} import ..."))
                elif not mod and any(a.name == "cli" for a in node.names):
                    hits.append((node.lineno, f"from {'.' * node.level} import cli"))
                continue
            if mod == "mind_meld.cli" or mod.startswith("mind_meld.cli."):
                hits.append((node.lineno, f"from {mod} import ..."))
            elif mod == "mind_meld" and any(a.name == "cli" for a in node.names):
                hits.append((node.lineno, "from mind_meld import cli"))
    return hits


# Every import shape that re-creates the cycle, plus controls that must NOT
# trip. Keeping this as data makes the detector's coverage auditable, and
# `test_cycle_detector_is_not_vacuous` proves each row still behaves.
_CYCLE_SHAPES = [
    ("from mind_meld.cli import _error\n", True),
    ("from mind_meld.cli.sub import x\n", True),
    ("import mind_meld.cli\n", True),
    ("import mind_meld.cli as c\n", True),
    ("from mind_meld import cli\n", True),
    ("from mind_meld import cli as c\n", True),
    ("from .cli import _error\n", True),
    ("from . import cli\n", True),
    ("def f():\n    from mind_meld import cli\n", True),
    ("def f():\n    from .cli import _error\n", True),
    ("def f():\n    import mind_meld.cli\n", True),
    # Controls: siblings and leaves are fine.
    ("from mind_meld.safety import safe_str\n", False),
    ("from mind_meld import resolveflow\n", False),
    ("from .safety import safe_str\n", False),
    ("from mind_meld.client import x\n", False),
    # Store is nested: both parent-relative forms must remain detectable.
    ("from .. import cli\n", True),
    ("from ..cli import _error\n", True),
    ("def f():\n    from .. import cli\n", True),
    ("def f():\n    from ..cli import _error\n", True),
    ("from . import envelope\n", False),
    ("from .. import crypto\n", False),
]


@pytest.mark.parametrize("source,should_flag", _CYCLE_SHAPES)
def test_cycle_detector_is_not_vacuous(source: str, should_flag: bool, tmp_path: Path) -> None:
    """The detector flags every cycle shape and no sibling import.

    A structural gate that cannot fail is worse than no gate: it reads as
    coverage. Relative-import and function-local forms are in here because both
    slipped past the first version of this check.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    hits = _imports_cli(probe)
    assert bool(hits) is should_flag, f"{source!r} -> {hits}"


@pytest.mark.parametrize("mod", EXTRACTED)
def test_extracted_module_never_imports_cli(mod: str) -> None:
    hits = _imports_cli(SRC / f"{mod.replace('.', '/')}.py")
    assert hits == [], f"{mod}.py imports cli at {hits} — that is the cycle, not a workaround"


@pytest.mark.parametrize("mod", LEAVES)
def test_leaf_modules_import_nothing_from_cli(mod: str) -> None:
    """Leaves are the cycle-break. If one grows a cli import the break is gone."""
    hits = _imports_cli(SRC / f"{mod}.py")
    assert hits == [], f"leaf {mod}.py must not import cli (found {hits})"


_MAY_CREATE_CALLEES = {"_ensure_retro_skill_links", "_skill_links_check_due"}


def _calls_missing_may_create(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, func) for src/ calls that omit the consent kwarg."""
    hits: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        if name not in _MAY_CREATE_CALLEES:
            continue
        if not any(kw.arg == "may_create" for kw in node.keywords):
            hits.append((node.lineno, name))
    return hits


def test_src_skill_link_calls_pass_may_create() -> None:
    """The writers require may_create; every src/ call must still spell it.

    A forgotten production kwarg is a TypeError. This AST gate is the
    belt-and-braces check that also catches a call site the suite never
    invokes. Tests must pass it too (None still means allow-all).
    """
    offenders = {
        str(p.relative_to(SRC)): hits
        for p in SRC.rglob("*.py")
        if (hits := _calls_missing_may_create(p))
    }
    assert offenders == {}, f"src/ calls missing may_create: {offenders}"


def test_skill_link_does_not_import_config() -> None:
    """consented_agent_keys takes resolved sources; skill_link stays a near-leaf."""
    src = (SRC / "skill_link.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name != "mind_meld.config" and not a.name.startswith(
                    "mind_meld.config."
                ), f"skill_link imports config at line {node.lineno}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert mod != "mind_meld.config" and not mod.startswith("mind_meld.config."), (
                f"skill_link imports config at line {node.lineno}"
            )
            if mod == "mind_meld":
                assert all(a.name != "config" for a in node.names), (
                    f"skill_link imports config at line {node.lineno}"
                )


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


def test_canonical_for_conflict_has_one_owner() -> None:
    """Track 48A: the final-infix owner lives in manifest, not a third copy.

    Prefix globbing is not ownership. A second reconstruction in cli or
    resolveflow is how notes.md would again delete notes.sync-conflict-log.md.
    """
    from mind_meld import cli, manifest, resolveflow

    assert hasattr(manifest, "_canonical_for_conflict")
    assert cli.manifest._canonical_for_conflict is manifest._canonical_for_conflict
    assert resolveflow.manifest._canonical_for_conflict is manifest._canonical_for_conflict
    assert "def _canonical_for_conflict" not in (SRC / "resolveflow.py").read_text(encoding="utf-8")
    assert "def _canonical_for_conflict" not in (SRC / "cli.py").read_text(encoding="utf-8")
    assert "_canonical_for_conflict" not in resolveflow.__all__
    cli_src = (SRC / "cli.py").read_text(encoding="utf-8")
    resolveflow_src = (SRC / "resolveflow.py").read_text(encoding="utf-8")
    assert "manifest._canonical_for_conflict" in cli_src
    assert "manifest._canonical_for_conflict" in resolveflow_src
    assert "from mind_meld.resolveflow import _canonical_for_conflict" not in cli_src


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

    for real in (
        Path("~/.claude/skills/retro-fleet").expanduser(),
        Path("~/.codex/skills/retro-fleet").expanduser(),
        Path("~/.config/mind-meld").expanduser(),
        Path("~/.local/share/mind-meld/agent-skills").expanduser(),
    ):
        assert skill_link._is_real_agent_dir_under_pytest(real), real

    # A redirected target is fine -- the guard matches the registry's canonical
    # agent paths, not "anything under $HOME". A developer whose TMPDIR lives
    # under $HOME must not see every skill-link test fail with a message
    # accusing the test of being unisolated when it is correctly using tmp_path.
    assert not skill_link._is_real_agent_dir_under_pytest(
        tmp_path / "agents" / "claude" / "skills" / "retro-fleet"
    )
    assert not skill_link._is_real_agent_dir_under_pytest(
        Path("~/some-unrelated-dir/skills").expanduser()
    )


def test_real_home_guard_warns_instead_of_raising(capsys) -> None:
    """The guard must not raise: it sits on the `mm push` path.

    `_push_core` calls `_ensure_retro_skill_links` with no try/except, so an
    AssertionError here would abort a real push for anyone whose environment
    carries PYTEST_CURRENT_TEST (a pytest-driven harness, a hook fired from
    inside a test run, an inherited subprocess env). The precedent it follows,
    `crypto.store_passphrase_in_keyring`, returns False rather than raising.
    """
    from mind_meld import skill_link

    skill_link._refuse_real_home_under_pytest(Path("~/.claude/skills/retro-fleet").expanduser())
    assert "refusing to touch" in capsys.readouterr().err


def test_installer_skips_the_write_when_the_guard_trips(monkeypatch, tmp_path: Path) -> None:
    """Warning is not enough -- the write itself must not happen.

    The production installer is ``_ensure_retro_skill_links``. Tests redirect
    its roots via ``_TEST_SKILL_ROOT_OVERRIDES`` (and ``extra_rows``); this
    is the path every real ``mm push`` takes, not a one-agent adapter.
    """
    from mind_meld import skill_link

    monkeypatch.setattr(skill_link, "_is_real_agent_dir_under_pytest", lambda _t: True)
    skill_link._ensure_retro_skill_links(may_create=None)
    for descriptor in skill_link._skill_target_descriptors():
        assert not descriptor.target.exists()
    store = skill_link._skill_store_dir()
    assert not (store / "SKILL.md").exists()


def test_real_home_guard_is_inert_outside_pytest(monkeypatch) -> None:
    """With PYTEST_CURRENT_TEST unset the guard must be a total no-op.

    This is the branch every REAL `mm push` takes. `_ensure_retro_skill_links`
    consults the guard before doing anything, so a predicate that answered True
    outside pytest would silently stop the skill installer for the whole fleet
    AND print "refusing to touch ... from a test" on every push. Nothing
    exercised it: the suite only ever runs WITH the variable set, so the
    early-return was covered by exactly zero tests.
    """
    from mind_meld import skill_link

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    for real in (
        Path("~/.claude/skills/retro-fleet").expanduser(),
        Path("~/.codex/skills/retro-fleet").expanduser(),
        Path("~/.config/opencode/skills/retro-fleet").expanduser(),
        Path("~/.config/mind-meld").expanduser(),
    ):
        assert skill_link._is_real_agent_dir_under_pytest(real) is False, real


def test_real_home_guard_normalizes_without_following_symlinks(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """`abspath`, NOT `resolve` — the distinction the docstring calls load-bearing.

    In the steady state `~/.claude/skills/retro-fleet` IS a symlink into the
    pipx venv, so `resolve()` follows it clean out of $HOME and the guard
    returns False for the exact case it exists to catch. `test_real_home_guard_
    fires` cannot see this: on CI (and any machine without the link installed)
    the path does not exist, so `resolve()` and `abspath()` agree and the
    assertion passes either way. Rehome the guard onto tmp_path so the symlink
    can actually be built.
    """
    from mind_meld import skill_link

    home = (tmp_path / "home").resolve()
    (home / ".claude" / "skills").mkdir(parents=True)
    elsewhere = tmp_path / "pipx-venv" / "retro_fleet"
    elsewhere.mkdir(parents=True)
    link = home / ".claude" / "skills" / "retro-fleet"
    link.symlink_to(elsewhere)
    monkeypatch.setattr(skill_link, "_REAL_HOME", home)

    assert skill_link._is_real_agent_dir_under_pytest(link), (
        "guard followed the symlink out of $HOME — it must use abspath, not resolve"
    )
    # `..` still gets normalized away; abspath is not "do nothing".
    assert skill_link._is_real_agent_dir_under_pytest(
        home / ".claude" / "skills" / ".." / "skills" / "retro-fleet"
    )
    # And it still refuses to over-match a sibling of the known roots.
    assert not skill_link._is_real_agent_dir_under_pytest(home / ".claude-backup" / "skills")

    skill_link._refuse_real_home_under_pytest(link)
    assert "refusing to touch" in capsys.readouterr().err


def test_real_home_guard_is_silent_on_an_isolated_path(tmp_path: Path, capsys) -> None:
    """The non-matching branch must print NOTHING.

    `_refuse_real_home_under_pytest` runs on every `_touch_marker` and every
    `_ensure_retro_skill_links` call, which is most of the suite. An inverted
    predicate would bury 2,000 tests' captured stderr in false notices — and
    `test_real_home_guard_warns_instead_of_raising` only asserts the positive
    direction, so it would stay green.
    """
    from mind_meld import skill_link

    skill_link._refuse_real_home_under_pytest(tmp_path / "agents" / "claude" / "skills")
    assert capsys.readouterr().err == ""


def test_touch_marker_refuses_the_real_config_dir(monkeypatch, capsys) -> None:
    """`_touch_marker` guards the WRITE LOCATION, not just its usual caller.

    The marker dir is `~/.config/mind-meld`, which conftest redirects — but a
    test that opts out of that fixture (this file's sibling `test_skill_link.py`
    does, deliberately), or any future path reaching `_touch_marker` without
    going through `_ensure_retro_skill_links`, would otherwise touch the
    developer's real config dir with the guard silent.
    """
    import uuid

    from mind_meld import skill_link

    real = Path("~/.config/mind-meld").expanduser()
    # Unique per run: a name a previous (or mutation-testing) run could have
    # left behind would poison the assertion forever, and the cleanup itself
    # would have to write the very directory this test refuses to touch.
    probe = f"mm-boundaries-probe-{uuid.uuid4().hex}"
    monkeypatch.setattr(skill_link, "_marker_dir", lambda: real)
    skill_link._touch_marker(probe)
    assert "refusing to touch" in capsys.readouterr().err
    assert not (real / f".{probe}").exists()


def test_skill_roots_are_redirected_for_this_test() -> None:
    """conftest's autouse fixture is actually in effect (not silently skipped)."""
    from mind_meld import skill_link

    overrides = skill_link._TEST_SKILL_ROOT_OVERRIDES
    assert overrides, "fixture should have populated the override map"
    assert all(not r.startswith("~") for r in overrides.values()), overrides
    assert all(row.skills_root.startswith("~/") for row in skill_link.AGENT_ROWS)


def test_skill_marker_dir_is_redirected_for_this_test() -> None:
    """The other half of `_isolate_skill_links`, which nothing pinned.

    The override map moves where the SYMLINKS go; `_marker_dir` moves where the
    TTL marker dotfiles go. Only the first had a non-vacuity check, so the
    fixture could half-break — leaving every test that runs the installer
    stamping `.skill-link-checked` into the developer's real
    `~/.config/mind-meld` — and the suite would stay green.
    """
    from mind_meld import skill_link

    assert skill_link._marker_dir() != Path("~/.config/mind-meld").expanduser()


def test_skill_store_dir_is_redirected_for_this_test() -> None:
    """Post-B: a store leak mutates the SKILL.md every agent executes."""
    from mind_meld import skill_link

    real = Path("~/.local/share/mind-meld/agent-skills/retro-fleet").expanduser()
    assert skill_link._skill_store_dir() != real


def test_agent_rows_are_tilde_relative_and_unique() -> None:
    """Hard assertions live in tests; import must never raise (T3)."""
    from mind_meld import skill_link

    rows = skill_link.AGENT_ROWS
    assert rows, "AGENT_ROWS must not be empty"
    keys = [row.key for row in rows]
    roots = [row.skills_root for row in rows]
    names = [row.display_name for row in rows]
    markers = [m for row in rows for m in (row.success_marker, row.conflict_marker)]
    assert all(root.startswith("~/") for root in roots), roots
    assert len(keys) == len(set(keys)), keys
    assert len(roots) == len(set(roots)), roots
    assert len(names) == len(set(names)), names
    assert len(set(markers)) == 2 * len(rows), markers


def test_marker_literals_are_byte_identical_to_v0_12_18() -> None:
    """A rename silently resets every fleet machine's 24h TTL (T5)."""
    from mind_meld import skill_link

    assert skill_link._SKILL_LINK_SUCCESS_MARKER == "skill-link-checked"
    assert skill_link._SKILL_LINK_CONFLICT_MARKER == "skill-link-conflict"
    assert skill_link._CODEX_SKILL_LINK_SUCCESS_MARKER == "codex-skill-link-checked"
    assert skill_link._CODEX_SKILL_LINK_CONFLICT_MARKER == "codex-skill-link-conflict"
    # Claude's success name is a suffix of the others, so startswith is unusable.
    names = [
        skill_link._SKILL_LINK_SUCCESS_MARKER,
        skill_link._SKILL_LINK_CONFLICT_MARKER,
        skill_link._CODEX_SKILL_LINK_SUCCESS_MARKER,
        skill_link._CODEX_SKILL_LINK_CONFLICT_MARKER,
    ]
    assert len(set(names)) == 4, names


def test_leading_agent_keys_are_stable() -> None:
    """Active supported agents; removal is a deliberate retirement, not a
    reordering. Claude and Codex stay at the front. Do not rewrite this
    to a two-key literal list — the AST gate's exemption is a slice, and
    these assertions use index access so they never need it."""
    from mind_meld import skill_link

    assert skill_link.AGENT_ROWS[0].key == "claude"
    assert skill_link.AGENT_ROWS[1].key == "codex"
    assert "opencode" not in {row.key for row in skill_link.AGENT_ROWS}


def test_production_overrides_default_is_empty() -> None:
    src = (SRC / "skill_link.py").read_text(encoding="utf-8")
    assert "_TEST_SKILL_ROOT_OVERRIDES: dict[str, str] = {}" in src


def test_descriptor_for_names_known_keys() -> None:
    from mind_meld import skill_link

    claude = skill_link._descriptor_for("claude")
    assert claude.key == "claude"
    assert claude.display_name == "Claude Code"
    with pytest.raises(KeyError, match="known keys:") as raised:
        skill_link._descriptor_for("not-an-agent")
    msg = str(raised.value)
    for row in skill_link.AGENT_ROWS:
        assert row.key in msg


def test_real_home_guard_covers_every_registry_row() -> None:
    """Expectation is computed independently of ``_home_relative`` (T2)."""
    from mind_meld import skill_link

    for row in skill_link.AGENT_ROWS:
        expected = Path(row.skills_root.replace("~", str(skill_link._REAL_HOME), 1))
        assert skill_link._is_real_agent_dir_under_pytest(expected), row.key


def test_malformed_skills_root_makes_guard_overmatch(monkeypatch) -> None:
    """A non-``~/``-relative root must over-match, never go blind (T2, T3)."""
    from mind_meld import skill_link

    bad = skill_link.AgentRow(
        key="bad",
        display_name="Bad Agent",
        skills_root="/opt/not-home-relative/skills",
        success_marker="bad-skill-link-checked",
        conflict_marker="bad-skill-link-conflict",
        consent_source="bad",
    )
    monkeypatch.setattr(skill_link, "AGENT_ROWS", (*skill_link.AGENT_ROWS, bad))
    sibling = skill_link._REAL_HOME / "some-unrelated-dir" / "skills"
    assert skill_link._is_real_agent_dir_under_pytest(sibling)


def test_redirect_creates_agent_dir_but_not_skills_dir(tmp_path: Path) -> None:
    from mind_meld import skill_link

    for row in skill_link.AGENT_ROWS:
        agent_dir = tmp_path / "agents" / row.key
        skills_dir = agent_dir / "skills"
        assert agent_dir.is_dir(), row.key
        assert not skills_dir.exists(), row.key


def test_redirect_gives_each_agent_its_own_dir(tmp_path: Path) -> None:
    from mind_meld import skill_link

    dirs = [(tmp_path / "agents" / row.key).resolve() for row in skill_link.AGENT_ROWS]
    assert len(dirs) == len(set(dirs)), dirs


def test_orphan_override_key_emits_notice(monkeypatch, capsys) -> None:
    from mind_meld import skill_link

    monkeypatch.setattr(skill_link, "_ORPHAN_OVERRIDE_WARNED", set())
    patched = dict(skill_link._TEST_SKILL_ROOT_OVERRIDES)
    patched["no-such-agent"] = "/tmp/nowhere"
    monkeypatch.setattr(skill_link, "_TEST_SKILL_ROOT_OVERRIDES", patched)
    skill_link._skill_target_descriptors()
    err = capsys.readouterr().err
    assert "orphan key 'no-such-agent'" in err


def test_mm_skills_dir_is_rejected_outside_pytest(monkeypatch, tmp_path, capsys) -> None:
    from mind_meld import skill_link

    monkeypatch.setattr(skill_link, "_MM_SKILLS_DIR_REJECTION_EMITTED", False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("MM_SKILLS_DIR", str(tmp_path / "should-not-be-used"))
    store = skill_link._skill_store_dir()
    assert store == Path("~/.local/share/mind-meld/agent-skills/retro-fleet").expanduser()
    assert "MM_SKILLS_DIR is a test-only override" in capsys.readouterr().err


def test_synthetic_row_is_covered_with_no_other_edit(monkeypatch, tmp_path) -> None:
    """The instrument that keeps the one-row claim honest (S4, T4)."""
    from typer.testing import CliRunner

    from mind_meld import skill_link
    from mind_meld.cli import app
    from tests.conftest import redirect_skill_paths

    synthetic = skill_link.AgentRow(
        key="synthetic",
        display_name="Synthetic Agent",
        skills_root="~/.mm-synthetic-agent/skills",
        success_marker="synthetic-skill-link-checked",
        conflict_marker="synthetic-skill-link-conflict",
        consent_source="synthetic",
    )
    redirect_skill_paths(monkeypatch, tmp_path, extra_rows=(synthetic,))
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", tmp_path / "no-such-config.toml")

    redirected = skill_link._TEST_SKILL_ROOT_OVERRIDES["synthetic"]
    assert redirected.startswith(str(tmp_path)), redirected
    assert not redirected.startswith(str(skill_link._REAL_HOME))
    synth_skills = Path(redirected)
    assert synth_skills.parent.is_dir()
    assert not synth_skills.exists()

    descriptors = skill_link._skill_target_descriptors()
    keys = [d.key for d in descriptors]
    assert keys[0] == "claude"
    assert keys[1] == "codex"
    assert keys[-1] == "synthetic"
    synth_desc = skill_link._descriptor_for("synthetic")
    assert synth_desc.display_name == "Synthetic Agent"
    assert str(synth_desc.skills_dir).startswith(str(tmp_path))

    targets = skill_link.skill_targets()
    assert synth_desc.target in targets
    assert skill_link._skill_links_check_due(may_create=None) is True

    results = skill_link._ensure_retro_skill_links(may_create=None)
    by_key = {r.descriptor.key: r for r in results}
    assert "synthetic" in by_key
    assert by_key["synthetic"].status == "installed"
    assert synth_desc.target.is_symlink()
    assert skill_link._skill_links_check_due(may_create=None) is False

    # An absent target is a user removal (Track 28A): the gate stays shut and
    # push leaves it alone. Explicit install is the documented recovery, and a
    # new registry row must be covered by that path too.
    synth_desc.target.unlink()
    assert skill_link._skill_links_check_due(may_create=None) is False
    skill_link._ensure_retro_skill_links(explicit=True, may_create=None)
    assert synth_desc.target.is_symlink()

    healthy = skill_link.diagnose_skill_links()
    healthy_synth = next(row for row in healthy if row["key"] == "synthetic")
    assert healthy_synth["agent"] == "Synthetic Agent"
    assert healthy_synth["status"] == "ok"
    assert "key" in healthy_synth

    original = skill_link._diagnose_one

    def boom(descriptor, *args, **kwargs):
        if descriptor.key == "synthetic":
            raise RuntimeError("forced diagnose error")
        return original(descriptor, *args, **kwargs)

    monkeypatch.setattr(skill_link, "_diagnose_one", boom)
    errored = skill_link.diagnose_skill_links()
    error_synth = next(row for row in errored if row.get("key") == "synthetic")
    assert error_synth["status"] == "error"
    assert error_synth["key"] == "synthetic"
    monkeypatch.setattr(skill_link, "_diagnose_one", original)

    import shutil

    for row in skill_link.AGENT_ROWS:
        agent_dir = tmp_path / "agents" / row.key
        if agent_dir.exists():
            shutil.rmtree(agent_dir)
    invoked = CliRunner().invoke(app, ["install-skills"])
    assert invoked.exit_code == 1
    assert "no supported agent skills directory exists" in invoked.output
    assert "Claude Code, Codex, and OpenCode" not in invoked.output

    canonical = skill_link._REAL_HOME / ".mm-synthetic-agent" / "skills"
    assert skill_link._is_real_agent_dir_under_pytest(canonical)
    assert not Path("~/.mm-synthetic-agent").expanduser().exists()


def test_no_consumer_owned_agent_name_lists() -> None:
    """A parallel agent-name list is the failure class this Track exists to kill."""
    import ast
    import re

    from mind_meld import skill_link

    keys = {row.key for row in skill_link.AGENT_ROWS}
    names = {row.display_name for row in skill_link.AGENT_ROWS}
    files = [
        SRC / "skill_link.py",
        SRC / "cli.py",
        SRC / "core.py",
        SRC / "statusflow.py",
        Path(__file__).resolve().parent / "conftest.py",
        Path(__file__).resolve().parent / "test_skill_link.py",
        Path(__file__),
    ]
    offenders: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                continue
            vals: list[str] = []
            ok = True
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    vals.append(elt.value)
                else:
                    ok = False
                    break
            if not ok or len(vals) < 2:
                continue
            literal_values = set(vals)
            if not (literal_values <= keys or literal_values <= names):
                continue
            src_line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            if re.search(r"\[:\d+\]", src_line):
                continue
            offenders.append(f"{path.name}:{node.lineno}:{src_line.strip()}")
    assert offenders == [], "parallel agent-name list:\n" + "\n".join(offenders)


def test_sidecar_and_lock_are_redirected_for_this_test(tmp_path: Path) -> None:
    """conftest's `_isolate_sidecar_and_lock` is in effect (not silently skipped).

    47 tests were writing the developer's real
    `~/.config/mind-meld/last-push.json` before Track 16A made this autouse, and
    the suite was rewriting `last-autorun.json` to "now" — forging the exact
    signal `mm status`'s staleness gate exists to produce. The fixture is the
    fix; this is the check that the fixture still works, mirroring
    `test_skill_roots_are_redirected_for_this_test` one section up.
    """
    from mind_meld import config, lockfile, sidecar

    real = Path("~/.config/mind-meld").expanduser()
    assert sidecar.SIDECAR_DIR != real
    assert config.LOCK_PATH != real / "mind-meld.lock"
    assert lockfile.LOCK_PATH == config.LOCK_PATH, "the two lock aliases drifted apart"


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
        "capture-cursor-usage",
        "conflicts",
        "cursor-agent",
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
        "recapture",
        "reconfigure-sources",
        "recover",
        "refresh-identity",
        "resolve",
        "retro-fleet",
        "sources",
        "status",
        "update",
    }


def test_statedb_import_allowlist():
    tree = ast.parse((SRC / "statedb.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] in sys.stdlib_module_names for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            if node.module == "mind_meld":
                assert [a.name for a in node.names] == ["fsutil"]
            else:
                assert node.module.split(".")[0] in sys.stdlib_module_names


# Track 83A: fixed ownership inventory, including shared types and constants.
CORE_DEFINITIONS = frozenset(
    """
    FetchStatus
    ManifestFetch
    _AUTORUN_VERBS
    _SYNC_OUTPUT
    _apply_verified_crypto_repair
    _autorun_breadcrumb_path
    _build_exclude_map
    _build_include_map
    _compute_recommended_excludes_diff
    _compute_recommended_includes_diff
    _config_missing_recommended_excludes
    _count
    _error
    _fetch_remote_manifest
    _filter_disabled_sources
    _filter_excluded_paths
    _filter_symlinked_paths
    _freeze_nested_checkout_entries
    _get_config
    _get_passphrase_or_exit
    _has_mtime_only_changes_vs_remote
    _home_relative_path
    _init_crypto_session
    _list_devices_warn
    _manifest_content_hash
    _merge_manifests
    _newer_format_message
    _normalize_autorun_breadcrumbs
    _pause_sync_progress
    _prove_omitted_paths_absent
    _require_published_source_root
    _resolve_all_configured_sources
    _retain_prior_default_sources
    _stdin_is_terminal
    iter_source_diffs
    """.split()
)

STATUSFLOW_DEFINITIONS = frozenset(
    """
    _BREADCRUMB_STALE_AFTER_HOURS
    _BROKEN_DIAG_STATUS_TO_INSTALL
    _CURSOR_DETAIL_NO_CLAUSE
    _CURSOR_DISABLE
    _CURSOR_ROW_CAUSES
    _DIAG_CAPTURE_CLAMP
    _DIAG_MODELS_SHOWN
    _DISCOVERY_REJECT_REASONS
    _breadcrumb_staleness_suffix
    _clamp_peer_text
    _collect_diag_state
    _collect_discovery_diag
    _collect_git_capture_diag
    _cursor_store_detail_clause
    _cursor_unsupported_clause
    _diag_models_line
    _fresh_discovery_label
    _host_complete_read_line
    _host_publication
    _host_read_budgets
    _host_read_sweep_line
    _host_usage_blocker
    _host_usage_module
    _print_host_publication
    _print_retro_capture_status
    _read_autorun_breadcrumbs
    _read_autorun_evidence
    _read_capture_rows
    _reader_capture_readiness
    _render_broken_skill_status
    _sanitize_recorded_capture
    _sqlite_version
    _status_notices
    _status_verdict
    _usage_capture_needs_upgrade
    _usage_capture_remedy
    diag
    status
    """.split()
)


def _top_level_definitions(source: str) -> set[str]:
    names = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(
                child.id
                for target in targets
                for child in ast.walk(target)
                if isinstance(child, ast.Name)
            )
    return names


def _unqualified_owner_calls(source: str, consumer: str) -> list[tuple[int, str]]:
    """Reject dead aliases while allowing names bound inside a local scope."""
    import symtable

    # The compiler's scope table distinguishes statusflow's nested _count
    # from the shared core helper, unlike a textual name search.
    scopes = symtable.symtable(source, "consumer.py", "exec")
    scope_by_location = {}

    def index(scope):
        for child in scope.get_children():
            scope_by_location[(child.get_name(), child.get_lineno())] = child
            index(child)

    index(scopes)
    owners = dict.fromkeys(CORE_DEFINITIONS, "core")
    owners.update(dict.fromkeys(STATUSFLOW_DEFINITIONS, "statusflow"))
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[-1]
            if module in {"core", "statusflow"}:
                for alias in node.names:
                    if owners.get(alias.name) == module:
                        owners[alias.asname or alias.name] = module
    hits = []

    class Calls(ast.NodeVisitor):
        scope = scopes

        def visit_FunctionDef(self, node):
            previous = self.scope
            self.scope = scope_by_location[(node.name, node.lineno)]
            for statement in node.body:
                self.visit(statement)
            self.scope = previous

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name):
                name = node.func.id
                if name in owners and owners[name] != consumer:
                    binding = self.scope.lookup(name)
                    if binding.is_global():
                        hits.append((node.lineno, name))
            self.generic_visit(node)

    Calls().visit(ast.parse(source))
    return hits


def test_status_core_single_ownership_and_qualified_calls():
    inventories = {"core": CORE_DEFINITIONS, "statusflow": STATUSFLOW_DEFINITIONS}
    moved = CORE_DEFINITIONS | STATUSFLOW_DEFINITIONS
    for module in ("cli", "core", "statusflow"):
        source = (SRC / f"{module}.py").read_text()
        expected = {"status", "diag"} if module == "cli" else inventories[module]
        assert _top_level_definitions(source) & moved == expected
        assert _unqualified_owner_calls(source, module) == []
    assert len(CORE_DEFINITIONS) == 35
    assert len(STATUSFLOW_DEFINITIONS) == 38


@pytest.mark.parametrize(
    "source,consumer,flags",
    [
        ("def caller(): _get_config(read_only=True)", "cli", True),
        ("def caller(): core._get_config(read_only=True)", "cli", False),
        ("def caller(): _status_verdict()", "cli", True),
        ("def caller(): statusflow._status_verdict()", "cli", False),
        ("def caller(): _get_config(read_only=True)", "statusflow", True),
        (
            "from mind_meld.core import _get_config as load\ndef caller(): load(read_only=True)",
            "statusflow",
            True,
        ),
        ("def caller(): _status_verdict()", "statusflow", False),
        ("def caller():\n def _count(): pass\n _count()", "statusflow", False),
    ],
)
def test_owner_call_gate_rejects_dead_aliases(source, consumer, flags):
    assert bool(_unqualified_owner_calls(source, consumer)) is flags


def test_core_does_not_import_statusflow():
    tree = ast.parse((SRC / "core.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name != "mind_meld.statusflow" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in {"mind_meld.statusflow", "statusflow"}
            if node.module == "mind_meld" or (node.level and not node.module):
                assert all(a.name != "statusflow" for a in node.names)


def test_statusflow_import_does_not_register_cli():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from mind_meld import statusflow; "
            "assert 'mind_meld.cli' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_core_sync_output_is_shared_with_cli():
    from mind_meld import cli, core

    assert cli._SYNC_OUTPUT is core._SYNC_OUTPUT


# ---------------------------------------------------------------------------
# Track 84A — Codex host reader adapter & host_registry boundaries.
# ---------------------------------------------------------------------------

CODEX_HOST_DEFINITIONS = frozenset(
    """
    CACHE_PATH
    CODEX_ARCHIVED_SESSIONS_PATH
    CODEX_SESSIONS_PATH
    _CacheEntry
    _ROLLOUT_NAME
    _Walk
    _archive_root_exists
    _cache_entry
    _cache_hit
    _carries_usage
    _context_model
    _context_turn
    _counter
    _counter_list_ok
    _counters
    _diag_model_ids
    _empty_cache
    _flush_pending
    _is_token_count
    _iter_archived_rollouts
    _iter_rollouts
    _no_ledger_entry
    _read_full_rollout
    _read_rollout
    _reading_from_record
    _record_day
    _resumable_entry
    _resume_rollout
    _same_cache_metadata
    _same_fingerprint
    _scan_codex_root
    _validated_counter_list
    _validated_entry
    _validated_pending
    _validated_turn_id
    _walk_from_entry
    codex_usage_diag
    read_codex_usage
    """.split()
)

HOST_REGISTRY_DEFINITIONS = frozenset(
    """
    HOST_READER_DIAGS
    HostReaderDiag
    reader_cache_cold
    reader_usage_diag
    warm_host_cache_inline
    """.split()
)

MOVED_HOST_DEFINITIONS = CODEX_HOST_DEFINITIONS | HOST_REGISTRY_DEFINITIONS

ALLOWED_HOST_ADAPTER_IMPORTS = frozenset(
    {"host_usage", "token_usage", "lockedjson", "fsutil", "errors"}
)


def test_codex_and_registry_single_ownership():
    from mind_meld import host_usage

    assert len(CODEX_HOST_DEFINITIONS) == 38
    assert len(HOST_REGISTRY_DEFINITIONS) == 5
    codex_defs = _top_level_definitions((SRC / "hosts" / "codex.py").read_text(encoding="utf-8"))
    registry_defs = _top_level_definitions((SRC / "host_registry.py").read_text(encoding="utf-8"))
    usage_defs = _top_level_definitions((SRC / "host_usage.py").read_text(encoding="utf-8"))
    assert codex_defs == CODEX_HOST_DEFINITIONS
    assert registry_defs == HOST_REGISTRY_DEFINITIONS
    assert usage_defs & MOVED_HOST_DEFINITIONS == set()
    for name in MOVED_HOST_DEFINITIONS:
        assert not hasattr(host_usage, name), f"{name} still present on host_usage"
        assert name not in host_usage.__all__, f"{name} still exported in host_usage.__all__"


def test_hosts_init_is_empty():
    init_path = SRC / "hosts" / "__init__.py"
    tree = ast.parse(init_path.read_text(encoding="utf-8"))
    non_doc = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    assert non_doc == [], f"hosts/__init__.py must stay empty; found {non_doc}"


def _host_adapter_import_violations(source: str) -> list[tuple[int, str]]:
    """Return (lineno, description) for disallowed imports in a hosts/*.py adapter."""
    stdlib = sys.stdlib_module_names | {"__future__"}
    hits: list[tuple[int, str]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in stdlib:
                    hits.append((node.lineno, f"import {alias.name}"))
                elif alias.name == "mind_meld.host_usage" and alias.asname:
                    hits.append((node.lineno, f"import {alias.name} as {alias.asname}"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                hits.append((node.lineno, f"relative import level={node.level}"))
                continue
            mod = node.module or ""
            top = mod.split(".")[0]
            if top in stdlib:
                continue
            if mod == "mind_meld":
                for alias in node.names:
                    if alias.name not in ALLOWED_HOST_ADAPTER_IMPORTS:
                        hits.append((node.lineno, f"from mind_meld import {alias.name}"))
                    elif alias.name == "host_usage" and alias.asname:
                        hits.append(
                            (
                                node.lineno,
                                f"from mind_meld import host_usage as {alias.asname}",
                            )
                        )
            elif mod.startswith("mind_meld."):
                sub = mod.removeprefix("mind_meld.")
                if sub not in (ALLOWED_HOST_ADAPTER_IMPORTS - {"host_usage"}):
                    hits.append((node.lineno, f"from {mod} import ..."))
            else:
                hits.append((node.lineno, f"from {mod} import ..."))
    return hits


@pytest.mark.parametrize("mod", HOST_ADAPTER_MODULES)
def test_host_adapter_import_allowlist(mod: str) -> None:
    path = SRC / f"{mod.replace('.', '/')}.py"
    hits = _host_adapter_import_violations(path.read_text(encoding="utf-8"))
    assert hits == [], f"{mod} has disallowed imports: {hits}"


def test_host_usage_never_imports_hosts_or_registry():
    tree = ast.parse((SRC / "host_usage.py").read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in {"mind_meld.hosts", "mind_meld.host_registry"} or a.name.startswith(
                    "mind_meld.hosts."
                ):
                    hits.append((node.lineno, a.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in {"mind_meld.hosts", "hosts", "mind_meld.host_registry", "host_registry"} or (
                mod.startswith("mind_meld.hosts.") or mod.startswith("hosts.")
            ):
                hits.append((node.lineno, mod))
            elif mod == "mind_meld" or (node.level and not mod):
                for a in node.names:
                    if a.name in {"hosts", "host_registry"}:
                        hits.append((node.lineno, a.name))
    assert hits == [], f"host_usage.py must never import hosts or host_registry: {hits}"


def test_host_registry_never_imports_cli_or_events_tail():
    tree = ast.parse((SRC / "host_registry.py").read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in {"mind_meld.cli", "mind_meld.events_tail"} or a.name.startswith(
                    ("mind_meld.cli.", "mind_meld.events_tail.")
                ):
                    hits.append((node.lineno, a.name))
                elif a.name == "mind_meld.host_usage" and a.asname:
                    hits.append((node.lineno, f"alias {a.asname}"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in {
                "mind_meld.cli",
                "cli",
                "mind_meld.events_tail",
                "events_tail",
                "mind_meld.host_usage",
                "host_usage",
            } or mod.startswith(("mind_meld.cli.", "mind_meld.events_tail.")):
                hits.append((node.lineno, mod))
            elif mod == "mind_meld" or (node.level and not mod):
                for a in node.names:
                    if a.name in {"cli", "events_tail"}:
                        hits.append((node.lineno, a.name))
                    elif a.name == "host_usage" and a.asname:
                        hits.append((node.lineno, f"alias {a.asname}"))
    assert hits == [], f"host_registry.py import violation: {hits}"


def test_hosts_and_registry_import_does_not_register_cli():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import mind_meld.hosts.codex; import mind_meld.host_registry; "
            "assert 'mind_meld.cli' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _unresolved_host_usage_attrs(source: str) -> list[tuple[int, str]]:
    from mind_meld import host_usage

    hits: list[tuple[int, str]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "host_usage"
            and not hasattr(host_usage, node.attr)
        ):
            hits.append((node.lineno, node.attr))
    return hits


@pytest.mark.parametrize("mod", ["host_registry", *HOST_ADAPTER_MODULES])
def test_every_qualified_host_usage_attribute_resolves(mod: str) -> None:
    path = SRC / f"{mod.replace('.', '/')}.py"
    hits = _unresolved_host_usage_attrs(path.read_text(encoding="utf-8"))
    assert hits == [], f"{mod} references missing host_usage attributes: {hits}"


def _stale_moved_references_or_patches(source: str) -> list[tuple[int, str]]:
    """Detect references/patches to moved Codex/registry names on host_usage aliases,
    plus one-sided clock patches in tests that call read_codex_usage."""
    tree = ast.parse(source)
    hu_aliases: set[str] = set()
    codex_aliases: set[str] = set()
    hits: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "mind_meld":
                for alias in node.names:
                    if alias.name == "host_usage":
                        hu_aliases.add(alias.asname or "host_usage")
                    elif alias.name in MOVED_HOST_DEFINITIONS:
                        hits.append((node.lineno, f"from mind_meld import {alias.name}"))
            elif node.module == "mind_meld.host_usage":
                for alias in node.names:
                    if alias.name in MOVED_HOST_DEFINITIONS:
                        hits.append((node.lineno, f"from mind_meld.host_usage import {alias.name}"))
            elif node.module == "mind_meld.hosts":
                for alias in node.names:
                    if alias.name == "codex":
                        codex_aliases.add(alias.asname or "codex")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "mind_meld.host_usage":
                    hu_aliases.add(alias.asname or alias.name)
                elif alias.name == "mind_meld.hosts.codex":
                    codex_aliases.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in MOVED_HOST_DEFINITIONS
            and isinstance(node.value, (ast.Name, ast.Attribute))
            and ast.unparse(node.value) in hu_aliases
        ):
            hits.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.Call):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    for moved in MOVED_HOST_DEFINITIONS:
                        if f"host_usage.{moved}" in arg.value:
                            hits.append((node.lineno, arg.value))
            if (
                len(node.args) >= 2
                and isinstance(node.args[0], (ast.Name, ast.Attribute))
                and ast.unparse(node.args[0]) in hu_aliases
            ):
                if (
                    isinstance(node.args[1], ast.Constant)
                    and node.args[1].value in MOVED_HOST_DEFINITIONS
                ):
                    hits.append(
                        (
                            node.lineno,
                            f"setattr/getattr({ast.unparse(node.args[0])}, {node.args[1].value!r})",
                        )
                    )
                elif isinstance(node.args[1], ast.JoinedStr):
                    pieces = "".join(
                        v.value
                        for v in node.args[1].values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str)
                    )
                    if any(
                        token in pieces
                        for token in ("_usage_diag", "_codex_usage", "_scan_", "_rollout")
                    ):
                        hits.append(
                            (
                                node.lineno,
                                f"dynamic setattr/getattr({ast.unparse(node.args[0])}, "
                                f"f'...{pieces}...')",
                            )
                        )

    # Replacing a module's clock binding must reach both the adapter and shared kit.
    # Patching an attribute on the shared time module already reaches both owners.
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls_codex = False
        patched_hu_clocks: set[str] = set()
        patched_codex_clocks: set[str] = set()
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Attribute) and sub.attr == "read_codex_usage":
                calls_codex = True
            elif (
                isinstance(sub, ast.Call)
                and len(sub.args) >= 2
                and isinstance(sub.args[0], (ast.Name, ast.Attribute))
                and isinstance(sub.args[1], ast.Constant)
                and sub.args[1].value in {"time", "datetime"}
            ):
                if ast.unparse(sub.args[0]) in hu_aliases:
                    patched_hu_clocks.add(sub.args[1].value)
                elif ast.unparse(sub.args[0]) in codex_aliases:
                    patched_codex_clocks.add(sub.args[1].value)
        if calls_codex:
            for clock in sorted(patched_hu_clocks ^ patched_codex_clocks):
                hits.append((fn.lineno, f"one-sided {clock} patch in {fn.name}"))
    return hits


def test_no_stale_host_usage_references_or_patches_across_repo():
    tests_dir = Path(__file__).resolve().parent
    offenders: dict[str, list[tuple[int, str]]] = {}
    for root in (SRC, tests_dir):
        for path in sorted(root.rglob("*.py")):
            if path == Path(__file__).resolve():
                continue
            hits = _stale_moved_references_or_patches(path.read_text(encoding="utf-8"))
            if hits:
                offenders[str(path.relative_to(SRC.parents[1]))] = hits
    assert offenders == {}, f"stale host_usage references or patches: {offenders}"


@pytest.mark.parametrize(
    "source,should_flag",
    [
        ("from mind_meld import host_usage, lockedjson, token_usage\n", False),
        ("from mind_meld import cli\n", True),
        ("from mind_meld import events_tail\n", True),
        ("from mind_meld import host_registry\n", True),
        ("from mind_meld.hosts import other\n", True),
        ("from mind_meld.host_usage import _expired\n", True),
        ("from mind_meld import host_usage as hu\n", True),
        ("from . import codex\n", True),
    ],
)
def test_host_adapter_import_gate_is_not_vacuous(source: str, should_flag: bool) -> None:
    assert bool(_host_adapter_import_violations(source)) is should_flag


@pytest.mark.parametrize(
    "source,should_flag",
    [
        ("host_usage._expired(1.0)\n", False),
        ("host_usage._nonexistent_helper_84a(1.0)\n", True),
    ],
)
def test_unresolved_host_usage_attr_gate_is_not_vacuous(source: str, should_flag: bool) -> None:
    assert bool(_unresolved_host_usage_attrs(source)) is should_flag


@pytest.mark.parametrize(
    "source,should_flag",
    [
        (
            "from mind_meld import host_usage as hu\nhu.read_muse_usage()\n",
            False,
        ),
        (
            "from mind_meld import host_usage as hu\nhu.read_codex_usage()\n",
            True,
        ),
        (
            "from mind_meld import host_usage as hu\nmonkeypatch.setattr(hu, 'CACHE_PATH', p)\n",
            True,
        ),
        (
            "from mind_meld import host_usage as hu\n"
            "monkeypatch.setattr(hu, f'{reader}_usage_diag', fn)\n",
            True,
        ),
        (
            "monkeypatch.setattr('mind_meld.host_usage.read_codex_usage', fn)\n",
            True,
        ),
        (
            "import mind_meld.host_usage\nmind_meld.host_usage.read_codex_usage()\n",
            True,
        ),
        (
            "import mind_meld.host_usage\n"
            "monkeypatch.setattr(mind_meld.host_usage, 'read_codex_usage', fn)\n",
            True,
        ),
        (
            "import mind_meld.host_usage\nimport mind_meld.hosts.codex\n"
            "def test_qualified_shared_clock(monkeypatch):\n"
            "    monkeypatch.setattr(mind_meld.host_usage, 'time', clock)\n"
            "    mind_meld.hosts.codex.read_codex_usage()\n",
            True,
        ),
        (
            "import mind_meld.host_usage\nimport mind_meld.hosts.codex\n"
            "def test_qualified_adapter_clock(monkeypatch):\n"
            "    monkeypatch.setattr(mind_meld.hosts.codex, 'datetime', clock)\n"
            "    mind_meld.hosts.codex.read_codex_usage()\n",
            True,
        ),
        (
            "import mind_meld.host_usage\nimport mind_meld.hosts.codex\n"
            "def test_qualified_dual_clock(monkeypatch):\n"
            "    monkeypatch.setattr(mind_meld.host_usage, 'time', clock)\n"
            "    monkeypatch.setattr(mind_meld.hosts.codex, 'time', clock)\n"
            "    mind_meld.hosts.codex.read_codex_usage()\n",
            False,
        ),
        (
            "from mind_meld import host_usage as hu\n"
            "from mind_meld.hosts import codex as codex_host\n"
            "def test_one_sided(monkeypatch):\n"
            "    monkeypatch.setattr(hu, 'time', clock)\n"
            "    codex_host.read_codex_usage()\n",
            True,
        ),
        (
            "from mind_meld import host_usage as hu\n"
            "from mind_meld.hosts import codex as codex_host\n"
            "def test_dual(monkeypatch):\n"
            "    monkeypatch.setattr(hu, 'time', clock)\n"
            "    monkeypatch.setattr(codex_host, 'time', clock)\n"
            "    codex_host.read_codex_usage()\n",
            False,
        ),
    ],
)
def test_stale_reference_and_clock_patch_gate_is_not_vacuous(
    source: str, should_flag: bool
) -> None:
    # Extended by the /ship test coverage audit.
    # Value: protects=fully qualified owner references obey the same extraction boundary;
    # fails_when=unaliased dotted imports evade stale-reference or one-sided clock checks;
    # why_new=existing seeds exercise aliases and string targets but not qualified modules;
    # seam=none; no_claim=runtime reader behavior; these are static gate counterexamples.
    assert bool(_stale_moved_references_or_patches(source)) is should_flag


@pytest.mark.parametrize("clock", ["time", "datetime"])
@pytest.mark.parametrize("owners", [("hu",), ("codex_host",), ("hu", "codex_host")])
def test_clock_patch_gate_checks_both_owners(clock: str, owners: tuple[str, ...]) -> None:
    # Value: protects=dual-owner clock replacement required by the adapter contract;
    # fails_when=either clock or patch direction escapes the boundary gate;
    # why_new=the original seeded rows covered only host_usage.time;
    # seam=none; no_claim=static gate coverage, not live reader timing.
    source = (
        "from mind_meld import host_usage as hu\n"
        "from mind_meld.hosts import codex as codex_host\n"
        "def test_read(monkeypatch):\n"
        + "".join(f"    monkeypatch.setattr({owner}, {clock!r}, clock)\n" for owner in owners)
        + "    codex_host.read_codex_usage()\n"
    )
    assert bool(_stale_moved_references_or_patches(source)) is (len(owners) == 1)
