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
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mind_meld.safety import safe_str

# Captured at import, BEFORE any test can monkeypatch $HOME, so the test-time
# guard below has a stable notion of "the developer's real home".
_REAL_HOME = Path(os.path.expanduser("~")).resolve()


def _is_real_agent_dir_under_pytest(target: Path) -> bool:
    """True when a TEST is about to write one of the real agent skills dirs.

    The installer mkdirs and symlinks into ``~/.claude/skills``,
    ``~/.codex/skills``, and ``~/.config/opencode/skills``. ``conftest.py`` had
    eight autouse isolation fixtures and none covered these, so any test driving
    ``_push_core`` or ``init`` without stubbing the installer mutated the
    developer's real agent config dirs — **67 tests**, measured.

    Matches the THREE KNOWN AGENT PATHS, not "anything under ``$HOME``". A
    developer whose ``TMPDIR`` lives under ``$HOME`` (common on macOS with a
    custom setting) would otherwise see every skill-link test fail with a
    message accusing the test of being unisolated when it is correctly using
    ``tmp_path``.

    Deliberately NOT a suite-wide ``monkeypatch.setenv("HOME", ...)``:
    ``importlib.metadata.version()`` resolves from the HOME-derived user
    site-packages, so a moved HOME degrades ``__version__`` to ``0.0.0+dev`` and
    trips ``_check_fleet_version_or_refuse`` in a dozen integration tests.

    Returns a bool rather than raising. The caller decides, because this runs on
    the ``mm push`` path: an ``AssertionError`` here would abort a real push for
    anyone whose environment happens to carry ``PYTEST_CURRENT_TEST`` (a
    pytest-driven harness, a hook fired from inside a test run, an inherited
    subprocess env). ``crypto.store_passphrase_in_keyring``, the precedent this
    follows, returns ``False`` for the same reason.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        # abspath, NOT resolve(): we care about the LOCATION BEING WRITTEN, not
        # where an existing symlink points. `~/.claude/skills/retro-fleet` is
        # normally already a symlink into the pipx venv, so `resolve()` follows
        # it clean out of $HOME and the guard silently returns False for the
        # exact case it exists to catch. abspath still normalizes "..".
        candidate = Path(os.path.abspath(target.expanduser()))
    except OSError:
        return False
    return any(
        candidate == real or real in candidate.parents
        for real in (
            (_REAL_HOME / ".claude" / "skills"),
            (_REAL_HOME / ".codex" / "skills"),
            (_REAL_HOME / ".config" / "opencode" / "skills"),
            (_REAL_HOME / ".config" / "mind-meld"),
        )
    )


def _refuse_real_home_under_pytest(target: Path) -> None:
    """Skip the write, loudly, when a test reaches a real agent skills dir.

    Emits to stderr and returns instead of raising, so the installer keeps its
    documented "forensic-only, never fail a push" contract. pytest surfaces the
    line in captured output, and `test_real_home_guard_fires` asserts on it.
    """
    if not _is_real_agent_dir_under_pytest(target):
        return
    sys.stderr.write(
        f"mm: notice: refusing to touch {target} from a test — this test reached "
        f"the real skill installer. Stub skill_link._ensure_retro_skill_links, or "
        f"mark the test with @pytest.mark.owns_skill_paths.\n"
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
# The descriptor registry below is derived from this seam on every call, so
# tests can redirect the three roots without freezing a developer's real HOME.
SKILL_ROOTS: tuple[str, str, str] = (
    "~/.claude/skills",
    "~/.codex/skills",
    "~/.config/opencode/skills",
)


SkillInstallStatus = Literal["installed", "unchanged", "unavailable", "conflict", "failed"]


@dataclass(frozen=True)
class SkillTarget:
    """One supported agent's retro-fleet installation contract.

    ``SKILL_ROOTS`` intentionally stays patchable for test isolation. These
    descriptors are therefore built at call time instead of freezing paths at
    import time.
    """

    display_name: str
    agent_root: Path
    skills_dir: Path
    success_marker: str
    conflict_marker: str

    @property
    def target(self) -> Path:
        return self.skills_dir / _SKILL_LINK_NAME


@dataclass(frozen=True)
class SkillInstallResult:
    """The observed outcome of one descriptor-driven install attempt."""

    descriptor: SkillTarget
    status: SkillInstallStatus
    skill_src: Path | None = None
    reason: str | None = None

    @property
    def target(self) -> Path:
        return self.descriptor.target


def _skill_target_descriptors() -> tuple[SkillTarget, ...]:
    """Return fresh descriptors so patched roots and ``$HOME`` take effect."""
    entries = (
        ("Claude Code", _SKILL_LINK_SUCCESS_MARKER, _SKILL_LINK_CONFLICT_MARKER),
        ("Codex", _CODEX_SKILL_LINK_SUCCESS_MARKER, _CODEX_SKILL_LINK_CONFLICT_MARKER),
        ("OpenCode", _OPENCODE_SKILL_LINK_SUCCESS_MARKER, _OPENCODE_SKILL_LINK_CONFLICT_MARKER),
    )
    return tuple(
        SkillTarget(
            display_name=display_name,
            agent_root=(skills_dir := Path(root).expanduser()).parent,
            skills_dir=skills_dir,
            success_marker=success_marker,
            conflict_marker=conflict_marker,
        )
        for root, (display_name, success_marker, conflict_marker) in zip(SKILL_ROOTS, entries)
    )


def _reason(error: BaseException) -> str:
    """Return the safe, stable failure detail surfaced by the CLI."""
    return f"{type(error).__name__}: {safe_str(error)}"


def _ensure_retro_skill_link(*, dry_run: bool = False) -> SkillInstallResult | None:
    """Compatibility adapter for Claude Code's descriptor-driven installer."""
    return _ensure_skill_target(_skill_target_descriptors()[0], dry_run=dry_run)


def _ensure_codex_retro_skill_link(*, dry_run: bool = False) -> SkillInstallResult | None:
    """Compatibility adapter for Codex's descriptor-driven installer."""
    return _ensure_skill_target(_skill_target_descriptors()[1], dry_run=dry_run)


def _ensure_opencode_retro_skill_link(*, dry_run: bool = False) -> SkillInstallResult | None:
    """Compatibility adapter for OpenCode's descriptor-driven installer."""
    return _ensure_skill_target(_skill_target_descriptors()[2], dry_run=dry_run)


def _ensure_retro_skill_links(*, dry_run: bool = False) -> tuple[SkillInstallResult, ...]:
    """Best-effort install for all three supported agent roots.

    Results are complete and ordered Claude Code, Codex, OpenCode. Expected
    filesystem failures are values, never exceptions, so init and push keep
    their best-effort contract while ``mm install-skills`` can report truthfully.
    """
    if dry_run:
        return ()

    descriptors = _skill_target_descriptors()
    results: list[SkillInstallResult | None] = [None] * len(descriptors)
    available: list[tuple[int, SkillTarget]] = []

    for index, descriptor in enumerate(descriptors):
        if _is_real_agent_dir_under_pytest(descriptor.target):
            _refuse_real_home_under_pytest(descriptor.target)
            results[index] = SkillInstallResult(
                descriptor,
                "failed",
                reason="refused to write a real agent directory from a test",
            )
            continue
        try:
            availability, failure = _agent_root_availability(descriptor)
        except Exception as error:
            results[index] = _failed_result(descriptor, "availability check", error)
            continue
        if availability == "unavailable":
            results[index] = SkillInstallResult(descriptor, "unavailable")
        elif availability == "failed":
            results[index] = _failed_result(descriptor, "availability check", failure)
        else:
            available.append((index, descriptor))

    if available:
        try:
            skill_src = _resolve_retro_skill_source_once()
        except Exception as error:
            for index, descriptor in available:
                results[index] = _failed_result(descriptor, "skill source resolution", error)
        else:
            for index, descriptor in available:
                try:
                    results[index] = _install_available_skill_target(descriptor, skill_src)
                except Exception as error:
                    results[index] = _failed_result(descriptor, "installation", error)

    return tuple(
        result
        if result is not None
        else _failed_result(
            descriptor,
            "installation",
            RuntimeError("installer produced no outcome"),
        )
        for descriptor, result in zip(descriptors, results)
    )


def _ensure_skill_target(
    descriptor: SkillTarget, *, dry_run: bool = False
) -> SkillInstallResult | None:
    """Run the legacy one-agent adapter without weakening its safety rules."""
    if dry_run:
        return None
    if _is_real_agent_dir_under_pytest(descriptor.target):
        _refuse_real_home_under_pytest(descriptor.target)
        return None

    try:
        availability, failure = _agent_root_availability(descriptor)
    except Exception as error:
        return _failed_result(descriptor, "availability check", error)
    if availability == "unavailable":
        return SkillInstallResult(descriptor, "unavailable")
    if availability == "failed":
        return _failed_result(descriptor, "availability check", failure)
    try:
        skill_src = _resolve_retro_skill_source_once()
    except Exception as error:
        return _failed_result(descriptor, "skill source resolution", error)
    return _install_available_skill_target(descriptor, skill_src)


def _ensure_retro_skill_link_at(
    target: Path,
    *,
    success_marker: str,
    conflict_marker: str,
    dry_run: bool = False,
) -> SkillInstallResult | None:
    """Backward-compatible one-target entry point used by safety tests."""
    descriptor = SkillTarget(
        display_name="Claude Code",
        agent_root=target.parent.parent,
        skills_dir=target.parent,
        success_marker=success_marker,
        conflict_marker=conflict_marker,
    )
    return _ensure_skill_target(descriptor, dry_run=dry_run)


def _agent_root_availability(
    descriptor: SkillTarget,
) -> tuple[Literal["available", "unavailable", "failed"], OSError | None]:
    """Probe the root without treating I/O errors as an absent agent."""
    try:
        info = descriptor.agent_root.stat()
    except FileNotFoundError:
        return "unavailable", None
    except OSError as error:
        return "failed", error
    if not stat.S_ISDIR(info.st_mode):
        return "failed", NotADirectoryError(f"{descriptor.agent_root} is not a directory")
    return "available", None


def _failed_result(
    descriptor: SkillTarget, operation: str, error: BaseException | None
) -> SkillInstallResult:
    """Create and emit a forensic failure result without aborting callers."""
    reason = _reason(error) if error is not None else "unknown failure"
    sys.stderr.write(
        f"mm: notice: {descriptor.display_name} retro-fleet {operation} failed: {reason}\n"
    )
    return SkillInstallResult(descriptor, "failed", reason=reason)


def _resolve_retro_skill_source_once() -> Path:
    """Resolve and validate the shared directory once per plural run."""
    skill_src = _resolve_retro_skill_src()
    source_info = skill_src.stat()
    if not stat.S_ISDIR(source_info.st_mode):
        raise NotADirectoryError(f"{skill_src} is not a directory")
    return skill_src.resolve(strict=True)


def _install_available_skill_target(descriptor: SkillTarget, skill_src: Path) -> SkillInstallResult:
    """Install one link after its agent root and shared source are known good."""
    target = descriptor.target
    try:
        skills_info = descriptor.skills_dir.stat()
    except FileNotFoundError:
        try:
            descriptor.skills_dir.mkdir(mode=0o700)
        except OSError as error:
            return _failed_result(descriptor, "skills directory setup", error)
    except OSError as error:
        return _failed_result(descriptor, "skills directory inspection", error)
    else:
        if not stat.S_ISDIR(skills_info.st_mode):
            return _failed_result(
                descriptor,
                "skills directory inspection",
                NotADirectoryError(f"{descriptor.skills_dir} is not a directory"),
            )

    try:
        target_info = target.lstat()
    except FileNotFoundError:
        target_info = None
    except OSError as error:
        return _failed_result(descriptor, "target inspection", error)

    if target_info is not None:
        if stat.S_ISLNK(target_info.st_mode):
            try:
                resolved_target = target.resolve(strict=True)
            except FileNotFoundError:
                # Do not unlink a dangling path. Another process could replace
                # it between resolution and cleanup, and unlinking would then
                # clobber the user's file or foreign symlink. A manual remove
                # is deliberate and preserves the installer no-clobber rule.
                _emit_conflict_notice(target, conflict_marker=descriptor.conflict_marker)
                return SkillInstallResult(descriptor, "conflict", skill_src=skill_src)
            except Exception as error:
                return _failed_result(descriptor, "target resolution", error)
            else:
                if resolved_target == skill_src:
                    _touch_marker(descriptor.success_marker)
                    return SkillInstallResult(descriptor, "unchanged", skill_src=skill_src)
                _emit_conflict_notice(target, conflict_marker=descriptor.conflict_marker)
                return SkillInstallResult(descriptor, "conflict", skill_src=skill_src)
        else:
            _emit_conflict_notice(target, conflict_marker=descriptor.conflict_marker)
            return SkillInstallResult(descriptor, "conflict", skill_src=skill_src)

    try:
        target.symlink_to(skill_src)
    except OSError as error:
        return _failed_result(descriptor, "link install", error)
    _touch_marker(descriptor.success_marker)
    return SkillInstallResult(descriptor, "installed", skill_src=skill_src)


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
    # Guard the WRITE LOCATION, not just one caller. Markers live in
    # ~/.config/mind-meld, which conftest redirects — but a test that opts out
    # of that fixture, or any future path reaching here without going through
    # _ensure_retro_skill_link_at, would otherwise write the developer's real
    # config dir with the guard silent.
    if _is_real_agent_dir_under_pytest(marker_dir):
        _refuse_real_home_under_pytest(marker_dir)
        return
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
    return _skill_link_check_due_for(_skill_target_descriptors()[0])


def _codex_skill_link_check_due() -> bool:
    """Return whether Codex's retro-fleet skill needs a self-heal attempt."""
    return _skill_link_check_due_for(_skill_target_descriptors()[1])


def _opencode_skill_link_check_due() -> bool:
    """Return whether OpenCode's retro-fleet skill needs a self-heal attempt."""
    return _skill_link_check_due_for(_skill_target_descriptors()[2])


def _skill_links_check_due() -> bool:
    """Return whether any supported agent's retro-fleet link has drifted."""
    return any(_skill_link_check_due_for(descriptor) for descriptor in _skill_target_descriptors())


def _skill_link_check_due_for(descriptor: SkillTarget) -> bool:
    """Return whether one descriptor needs a repair attempt.

    A missing agent root is the only quiet skip. A present root with no skills
    directory is immediately due, and every other inspection failure fails
    open so the plural installer can leave a forensic notice.
    """
    availability, _failure = _agent_root_availability(descriptor)
    if availability == "unavailable":
        return False
    if availability == "failed":
        return True
    try:
        skills_info = descriptor.skills_dir.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return True
    if not stat.S_ISDIR(skills_info.st_mode):
        return True
    return _skill_link_check_due_at(descriptor.target, success_marker=descriptor.success_marker)


def _skill_link_check_due_at(target: Path, *, success_marker: str) -> bool:
    """Target-specific implementation of the 24-hour skill-link drift gate."""
    if not _marker_is_fresh(success_marker):
        return True
    try:
        target_info = target.lstat()
        if not stat.S_ISLNK(target_info.st_mode):
            return True
        skill_src = _resolve_retro_skill_source_once()
        if target.resolve(strict=True) != skill_src:
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
    return tuple(descriptor.target for descriptor in _skill_target_descriptors())


__all__ = [
    "SKILL_LINK_TTL_SECONDS",
    "SKILL_ROOTS",
    "SkillInstallResult",
    "SkillTarget",
    "_refuse_real_home_under_pytest",
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
