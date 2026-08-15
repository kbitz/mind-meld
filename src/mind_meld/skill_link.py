"""Retro-fleet skill installer: symlinks, the 24h drift gate, and its markers.

Extracted from ``cli.py`` in Track 16A. `mm` keeps a ``retro-fleet`` skill
symlinked into each supported agent's skills directory (Claude Code, Codex,
OpenCode) and self-heals the link behind a 24-hour TTL gate on every push.

Imports nothing from ``cli`` — pinned by ``tests/test_module_boundaries.py``.

``_marker_dir()`` (was ``_config_dir()`` in cli.py) is deliberately a FUNCTION,
re-resolved per call, not a module-level constant. ``config.CONFIG_DIR`` is
``Path.home() / ...`` frozen at import, and ``CONFIG_PATH`` / ``LOCK_PATH``
derive from it at import too, so a test that setattrs one does not move the
others. Do NOT "simplify" this to ``config.CONFIG_DIR``; see
``docs/invariants/events-retro.md``.

Read ``docs/invariants/events-retro.md`` before editing the gate or the
installer branches.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from mind_meld.safety import safe_str

# Captured at import, BEFORE any test can monkeypatch $HOME, so the test-time
# guard below has a stable notion of "the developer's real home".
_REAL_HOME = Path(os.path.expanduser("~")).resolve()


def _refuse_real_home_under_pytest(target: Path) -> None:
    """Fail loudly if a test is about to symlink into the developer's real HOME.

    The installer mkdirs and symlinks into ``~/.claude/skills``,
    ``~/.codex/skills``, and ``~/.config/opencode/skills``. ``conftest.py`` has
    nine autouse isolation fixtures and none covered these, so any test that
    drove ``_push_core`` or ``init`` without stubbing the installer mutated the
    developer's real agent config dirs. Only ``test_skill_link.py`` isolates
    HOME, via its own local fixture.

    Same shape as the load-bearing ``PYTEST_CURRENT_TEST`` guard on
    ``crypto.store_passphrase_in_keyring``. Deliberately NOT a suite-wide
    ``monkeypatch.setenv("HOME", ...)``: ``importlib.metadata.version()``
    resolves from the HOME-derived user site-packages, so a moved HOME degrades
    ``__version__`` to ``0.0.0+dev`` and trips ``_check_fleet_version_or_refuse``
    in a dozen integration tests.

    Raises rather than silently skipping: a test that reaches the real installer
    is a bug in the test, and this Track is the one that would otherwise have
    made it invisible.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        resolved = target.expanduser().resolve()
    except OSError:
        return
    if resolved == _REAL_HOME or _REAL_HOME in resolved.parents:
        raise AssertionError(
            f"test reached the real skill installer: {resolved} is under the "
            f"developer's real HOME ({_REAL_HOME}). Stub "
            f"skill_link._ensure_retro_skill_links, or isolate HOME in the test."
        )


# Group 8 / Track 8A: 24h-TTL gate for the retro-fleet skill symlink installer.
# Two markers (cross-model #3 from /plan-eng-review): success caches the happy
# path; conflict-skip suppresses the per-push notice when the user has their
# own file at the target. Transient failure paths (OSError) leave both
# untouched so next push retries — matches the visible-failure contract.
SKILL_LINK_TTL_SECONDS = 24 * 60 * 60
_SKILL_LINK_NAME = "retro-fleet"
_SKILL_LINK_SUCCESS_MARKER = "skill-link-checked"
_SKILL_LINK_CONFLICT_MARKER = "skill-link-conflict"
_CODEX_SKILL_LINK_SUCCESS_MARKER = "codex-skill-link-checked"
_CODEX_SKILL_LINK_CONFLICT_MARKER = "codex-skill-link-conflict"
_OPENCODE_SKILL_LINK_SUCCESS_MARKER = "opencode-skill-link-checked"
_OPENCODE_SKILL_LINK_CONFLICT_MARKER = "opencode-skill-link-conflict"

# The three agent skills directories, as ~-relative strings. A module-level
# indirection rather than literals inline in each wrapper, so `conftest.py` can
# redirect all three with one setattr -- see `_isolate_skill_links` there.
#
# Strings, not Paths: `expanduser()` must run at CALL time, not import time.
# `config.CONFIG_DIR` is `Path.home() / ...` frozen at import and that is
# exactly the hazard Track 15B deleted two constants for.
#
# NOT the table-driven `_SKILL_TARGETS` consolidation Track 17A owns -- the six
# wrappers deliberately stay as they are. This is only the seam isolation needs.
SKILL_ROOTS: tuple[str, str, str] = (
    "~/.claude/skills",
    "~/.codex/skills",
    "~/.config/opencode/skills",
)


def _ensure_retro_skill_link(*, dry_run: bool = False) -> None:
    """Group 8 / Track 8A symlink self-heal for the retro-fleet skill.

    Three states (cross-model #3 from /plan-eng-review uses a 2-marker gate
    so deliberate-conflict skips don't spam stderr forever):

    * **success** — target absent OR target is a correct symlink at our
      skill source. Idempotent. Touch ``skill-link-checked`` marker.
    * **conflict-skip** — target exists as a real file or wrong symlink.
      Don't clobber the user's file. Emit ``mm: notice:`` once per 24h
      (gated by ``skill-link-conflict`` marker). User can ``rm`` to take
      mm's version.
    * **transient-failure** — TOCTOU FileExistsError, PermissionError on
      read-only ~/.claude, OSError on a filesystem without symlink
      support. CQ#1 forensic-only contract: emit ``mm: notice:``,
      return, leave both markers alone so next push retries.

    Dangling-symlink branch (Test review #1 IRON-RULE pin from
    /plan-eng-review): a symlink whose target was deleted (e.g., after
    ``pipx reinstall`` rebuilt the venv at a different path) is unlinked
    and replaced. Pre-fix, ``target.is_symlink() and target.resolve() ==
    src.resolve()`` skipped this case because resolve() returns the bad
    path; the second branch then matched ``target.is_symlink()`` and
    routed into "exists, don't replace" — silent permanent broken state.

    Called from ``mm init`` (always, no gate) and ``_push_core`` HEAD
    (24h-TTL gated). Both gates are read with ``os.stat`` wrapped in
    try/except (TODO#3 critical-gap fix: EACCES on the marker dir must
    fail-open so push doesn't crash).
    """
    _ensure_retro_skill_link_at(
        Path(SKILL_ROOTS[0]).expanduser() / _SKILL_LINK_NAME,
        success_marker=_SKILL_LINK_SUCCESS_MARKER,
        conflict_marker=_SKILL_LINK_CONFLICT_MARKER,
        dry_run=dry_run,
    )


def _ensure_codex_retro_skill_link(*, dry_run: bool = False) -> None:
    """Install the bundled retro-fleet skill for Codex when it is present."""
    _ensure_retro_skill_link_at(
        Path(SKILL_ROOTS[1]).expanduser() / _SKILL_LINK_NAME,
        success_marker=_CODEX_SKILL_LINK_SUCCESS_MARKER,
        conflict_marker=_CODEX_SKILL_LINK_CONFLICT_MARKER,
        dry_run=dry_run,
    )


def _ensure_opencode_retro_skill_link(*, dry_run: bool = False) -> None:
    """Install the bundled retro-fleet skill for OpenCode when it is present."""
    _ensure_retro_skill_link_at(
        Path(SKILL_ROOTS[2]).expanduser() / _SKILL_LINK_NAME,
        success_marker=_OPENCODE_SKILL_LINK_SUCCESS_MARKER,
        conflict_marker=_OPENCODE_SKILL_LINK_CONFLICT_MARKER,
        dry_run=dry_run,
    )


def _ensure_retro_skill_links(*, dry_run: bool = False) -> None:
    """Best-effort install for every supported global skill directory."""
    _ensure_retro_skill_link(dry_run=dry_run)
    _ensure_codex_retro_skill_link(dry_run=dry_run)
    _ensure_opencode_retro_skill_link(dry_run=dry_run)


def _ensure_retro_skill_link_at(
    target: Path,
    *,
    success_marker: str,
    conflict_marker: str,
    dry_run: bool = False,
) -> None:
    """Install the bundled skill at one agent-specific target.

    Claude Code, Codex, and OpenCode discover the same Agent Skills format
    from different global directories. Keeping target-specific markers separate
    means a missing installation of one agent never suppresses another's repair.
    """
    if dry_run:
        return

    _refuse_real_home_under_pytest(target)

    skills_dir = target.parent
    agent_dir = skills_dir.parent
    if not agent_dir.exists():
        # Silent skip — the agent is not installed. Touching the success marker
        # would suppress retries if the user installs it later in the day.
        return
    if not skills_dir.exists():
        try:
            skills_dir.mkdir(mode=0o700)
        except OSError as e:
            sys.stderr.write(
                f"mm: notice: retro-fleet skills directory setup failed: "
                f"{type(e).__name__}: {safe_str(e)}\n"
            )
            return

    try:
        skill_src = _resolve_retro_skill_src()
    except Exception as e:
        sys.stderr.write(
            f"mm: notice: retro-fleet skill source unresolvable: "
            f"{type(e).__name__}: {safe_str(e)}\n"
        )
        return

    # Branch 1: dangling symlink → unlink + recreate.
    # Path.exists() returns False on a dangling symlink while is_symlink()
    # returns True. This branch was missing in the original /plan-eng-review
    # design and is REGRESSION-class for pipx-reinstall recovery.
    if target.is_symlink() and not target.exists():
        try:
            target.unlink()
        except OSError as e:
            sys.stderr.write(
                f"mm: notice: retro-fleet skill dangling-link cleanup failed: "
                f"{type(e).__name__}: {safe_str(e)}\n"
            )
            return
        # Fall through to symlink_to creation below.
    # Branch 2: target is a correct, intact symlink to our source → no-op.
    elif target.is_symlink() and target.exists():
        try:
            if target.resolve() == skill_src.resolve():
                _touch_marker(success_marker)
                return
        except OSError:
            # resolve() can raise on a path with permission issues — fall
            # through to the conflict-skip branch.
            pass
        # Wrong target — user's own symlink elsewhere. Conflict-skip.
        _emit_conflict_notice(target, conflict_marker=conflict_marker)
        return
    # Branch 3: a real file or directory at the target → conflict-skip.
    elif target.exists():
        _emit_conflict_notice(target, conflict_marker=conflict_marker)
        return

    # Branch 4: target is absent (or just unlinked from dangling branch above).
    # Create the symlink.
    try:
        target.symlink_to(skill_src)
    except OSError as e:
        # CQ#1: TOCTOU FileExistsError, EACCES, EPERM, ENOTSUP — forensic
        # only. Don't crash push; don't touch markers; next push retries.
        sys.stderr.write(
            f"mm: notice: retro-fleet skill link install failed: "
            f"{type(e).__name__}: {safe_str(e)}\n"
        )
        return
    _touch_marker(success_marker)


def _resolve_retro_skill_src() -> Path:
    """Return the on-disk dir that the symlink should point at.

    Subtle: the on-disk dir is named ``retro_fleet`` (Python identifier) but
    the symlink target name is ``retro-fleet`` (Claude Code skill convention).
    The aggregator imports cleanly via ``mind_meld.skills.retro_fleet`` and
    Claude Code reads the symlinked dir as ``retro-fleet``.
    """
    import importlib.resources

    return Path(str(importlib.resources.files("mind_meld") / "skills" / "retro_fleet"))


def _emit_conflict_notice(
    target: Path, *, conflict_marker: str = _SKILL_LINK_CONFLICT_MARKER
) -> None:
    """Notice once per 24h — gated by the conflict marker. Cross-model #3
    from /plan-eng-review: per-push spam on a deliberate conflict is
    hostile; the gate suppresses repeats."""
    if _marker_is_fresh(conflict_marker):
        return
    sys.stderr.write(
        f"mm: notice: skill at {safe_str(str(target))} exists; not replacing "
        f"(remove the file to take mm's retro-fleet skill)\n"
    )
    _touch_marker(conflict_marker)


def _marker_is_fresh(name: str) -> bool:
    """Return True iff the marker exists AND its mtime is within
    ``SKILL_LINK_TTL_SECONDS``. TODO#3 critical-gap fix: stat failure
    fail-open (treat as if no marker — re-run installer)."""
    marker = _marker_dir() / f".{name}"
    try:
        st = marker.stat()
    except OSError:
        # FileNotFoundError, EACCES, EIO — fail-open. Returns False so the
        # caller runs the installer; matches the visible-failure contract
        # (no silent broken state).
        return False
    age = time.time() - st.st_mtime
    return age < SKILL_LINK_TTL_SECONDS


def _touch_marker(name: str) -> None:
    """Mtime-touch the named marker. Best-effort; OSError is swallowed
    silently (the next push will simply re-run the installer)."""
    marker_dir = _marker_dir()
    try:
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / f".{name}").touch()
    except OSError:
        pass


def _marker_dir() -> Path:
    return Path("~/.config/mind-meld").expanduser()


def _skill_link_check_due() -> bool:
    """Gate consulted by ``_push_core``. Returns True when the installer
    should run.

    Two paths to True:

    1. **Marker is stale** (or absent) — the original 24h-TTL behavior.
    2. **Marker is fresh but link state has drifted** — link is missing,
       dangling, or pointing somewhere other than our source. Pre-fix
       (post-v0.11.0 / pre-this-fix) the fresh marker silently suppressed
       self-heal for 24h. The case in the wild: pipx-installed mm 0.11.0
       creates the link successfully and touches the marker; user later
       removes the link manually (e.g. cleaning up an old conductor
       workspace whose path the link used to point at on a previous
       install); next push sees fresh marker + missing link and skips
       the installer for the rest of the day. The drift check costs one
       ``lstat`` + one ``readlink`` + ``importlib.resources`` resolution
       on the steady-state path — negligible vs the rest of push.

    Any I/O or resolver error in the drift check fails open (returns
    True) so the installer runs and emits its own notice. The conflict
    marker is consulted separately by ``_emit_conflict_notice``.
    """
    return _skill_link_check_due_at(
        Path(SKILL_ROOTS[0]).expanduser() / _SKILL_LINK_NAME,
        success_marker=_SKILL_LINK_SUCCESS_MARKER,
    )


def _codex_skill_link_check_due() -> bool:
    """Return whether Codex's retro-fleet skill needs a self-heal attempt."""
    target = Path(SKILL_ROOTS[1]).expanduser() / _SKILL_LINK_NAME
    if not target.parent.exists():
        return False
    return _skill_link_check_due_at(
        target,
        success_marker=_CODEX_SKILL_LINK_SUCCESS_MARKER,
    )


def _opencode_skill_link_check_due() -> bool:
    """Return whether OpenCode's retro-fleet skill needs a self-heal attempt."""
    target = Path(SKILL_ROOTS[2]).expanduser() / _SKILL_LINK_NAME
    if not target.parent.exists():
        return False
    return _skill_link_check_due_at(
        target,
        success_marker=_OPENCODE_SKILL_LINK_SUCCESS_MARKER,
    )


def _skill_links_check_due() -> bool:
    """Return whether any supported agent's retro-fleet link has drifted."""
    return (
        _skill_link_check_due() or _codex_skill_link_check_due() or _opencode_skill_link_check_due()
    )


def _skill_link_check_due_at(target: Path, *, success_marker: str) -> bool:
    """Target-specific implementation of the 24-hour skill-link drift gate."""
    if not _marker_is_fresh(success_marker):
        return True
    try:
        if not target.is_symlink():
            return True
        if not target.exists():
            return True  # dangling
        skill_src = _resolve_retro_skill_src()
        if target.resolve() != skill_src.resolve():
            return True  # wrong target (e.g. stale workspace path)
    except Exception:
        return True
    return False


def skill_targets() -> tuple[Path, ...]:
    """The three agent skill-link targets, in Claude / Codex / OpenCode order.

    Single source of truth for the target paths. ``install_skills_cmd`` used to
    rebuild this tuple from its own hardcoded string literals 3,000 lines away
    from the installer, so the two could drift; after the Track 16A cut they
    would have lived in different files under different Group 17 owners, and
    17A's charter is literally "installer correctness".

    Re-resolved per call (``expanduser`` reads ``$HOME`` at call time) so a test
    that moves HOME moves the targets with it.
    """
    return tuple(Path(parent).expanduser() / _SKILL_LINK_NAME for parent in SKILL_ROOTS)


def classify_targets(targets: tuple[Path, ...], skill_src: Path) -> tuple[list[Path], list[Path]]:
    """Split ``targets`` into ``(installed, conflicts)`` against ``skill_src``.

    ``installed`` = a symlink resolving to ``skill_src``. ``conflicts`` =
    anything else that exists (a real file, or a symlink pointing elsewhere).
    A target that does not exist at all appears in NEITHER list — that is the
    missing third bucket Track 17A adds; do not fix it here.
    """
    installed: list[Path] = []
    conflicts: list[Path] = []
    for target in targets:
        if target.is_symlink() and target.exists():
            try:
                if target.resolve() == skill_src.resolve():
                    installed.append(target)
                    continue
            except OSError:
                pass
        if target.exists() or target.is_symlink():
            conflicts.append(target)
    return installed, conflicts


__all__ = [
    "SKILL_LINK_TTL_SECONDS",
    "classify_targets",
    "skill_targets",
    "_codex_skill_link_check_due",
    "_ensure_codex_retro_skill_link",
    "_ensure_opencode_retro_skill_link",
    "_ensure_retro_skill_link",
    "_ensure_retro_skill_link_at",
    "_ensure_retro_skill_links",
    "_marker_dir",
    "_opencode_skill_link_check_due",
    "_resolve_retro_skill_src",
    "_skill_link_check_due",
    "_skill_link_check_due_at",
    "_skill_links_check_due",
]
