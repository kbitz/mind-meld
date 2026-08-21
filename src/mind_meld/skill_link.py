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

import fcntl
import hashlib
import json
import os
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from packaging.version import InvalidVersion, Version

from mind_meld import __version__
from mind_meld.errors import StorageError
from mind_meld.fsutil import atomic_write_bytes
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
            (_REAL_HOME / ".local" / "share" / "mind-meld" / "agent-skills"),
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


SkillInstallStatus = Literal[
    "installed",
    "unchanged",
    "unavailable",
    "dangling-ours",
    "dangling-ours-legacy",
    "foreign",
    "failed",
]

_STORE_PAYLOAD = "SKILL.md"
_STORE_META = ".mm-skill.json"
_STORE_SENTINEL = ".mm-owned"
_STORE_LOCK = ".publish.lock"
_LEGACY_TAIL = ("mind_meld", "skills", "retro_fleet")


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
    link_target: Path | None = None
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


def _skill_store_dir() -> Path:
    """Return the mm-owned skill store. Call-time seam — never expanduser at import.

    Override with ``MM_SKILLS_DIR`` for tests. The default sits next to the
    mm-events root but outside every ``DEFAULT_SOURCES`` ``include_dirs`` entry.
    """
    override = os.environ.get("MM_SKILLS_DIR")
    if override:
        return Path(override).expanduser()
    return Path("~/.local/share/mind-meld/agent-skills/retro-fleet").expanduser()


def _store_payload_path(store: Path | None = None) -> Path:
    return (store or _skill_store_dir()) / _STORE_PAYLOAD


def _store_is_healthy(store: Path | None = None) -> bool:
    payload = _store_payload_path(store)
    try:
        return payload.is_file() and payload.stat().st_size > 0
    except OSError:
        return False


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_store_meta(store: Path) -> dict | None:
    path = store / _STORE_META
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _pkg_skill_md(skill_src: Path) -> bytes:
    path = skill_src / _STORE_PAYLOAD
    data = path.read_bytes()
    if not data:
        raise FileNotFoundError(f"{path} is empty")
    return data


def _ensure_real_dir(path: Path, *, mode: int = 0o700) -> None:
    """Create ``path`` as a real directory, refusing a symlink or regular file."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=mode, parents=False)
        return
    if stat.S_ISLNK(info.st_mode):
        raise FileExistsError(f"refusing symlink at skill store path {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"refusing non-directory at skill store path {path}")


def _prepare_store_dir(store: Path) -> None:
    """Create the store directory with no-clobber checks. Raises on foreign trees."""
    parent = store.parent
    try:
        parent.lstat()
    except FileNotFoundError:
        grand = parent.parent
        try:
            grand_info = grand.lstat()
        except FileNotFoundError:
            grand.mkdir(mode=0o700, parents=True)
        else:
            if stat.S_ISLNK(grand_info.st_mode):
                raise FileExistsError(f"refusing symlink at {grand}")
            if not stat.S_ISDIR(grand_info.st_mode):
                raise NotADirectoryError(str(grand))
        _ensure_real_dir(parent)
    else:
        _ensure_real_dir(parent)

    try:
        info = store.lstat()
    except FileNotFoundError:
        _ensure_real_dir(store)
        atomic_write_bytes(store / _STORE_SENTINEL, b"mind-meld skill store\n", mode=0o644)
        return

    if stat.S_ISLNK(info.st_mode):
        raise FileExistsError(f"refusing symlink at skill store path {store}")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"refusing non-directory at skill store path {store}")

    names = {p.name for p in store.iterdir()}
    owned = _STORE_SENTINEL in names or _STORE_META in names or _STORE_PAYLOAD in names
    if names and not owned:
        raise FileExistsError(f"foreign non-empty skill store: {store}")
    if _STORE_SENTINEL not in names:
        atomic_write_bytes(store / _STORE_SENTINEL, b"mind-meld skill store\n", mode=0o644)


def _reject_payload_symlink(store: Path) -> None:
    payload = store / _STORE_PAYLOAD
    try:
        info = payload.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise FileExistsError(f"refusing to replace symlink payload at {payload}")


@contextmanager
def _store_publish_lock(store: Path) -> Iterator[None]:
    """Serialize publishers. ``init`` and ``install-skills`` do not hold the mm lock."""
    lock_path = store.parent / _STORE_LOCK
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _should_publish(pkg_version: str, pkg_hash: str, store: Path) -> tuple[bool, str | None]:
    """Return (publish?, equal-version-differing-hash notice)."""
    payload = store / _STORE_PAYLOAD
    meta = _read_store_meta(store)
    store_hash = None
    try:
        store_hash = _sha256_bytes(payload.read_bytes())
    except OSError:
        pass
    if meta is None or store_hash is None:
        return True, None
    try:
        pkg_v = Version(pkg_version)
        store_v = Version(str(meta.get("skill_version", "0")))
    except InvalidVersion:
        return True, None
    if pkg_v < store_v:
        return False, None
    if pkg_v > store_v:
        return True, None
    if pkg_hash != store_hash:
        return True, (
            "mm: notice: skill store and package share version "
            f"{pkg_version} but SKILL.md differs; republishing\n"
        )
    return False, None


def _publish_skill_store(skill_src: Path) -> Path:
    """Copy package SKILL.md into the store. Returns the store path.

    Publish-before-link invariant: callers must not re-point any agent link
    unless this returns and ``_store_is_healthy`` is True.
    """
    store = _skill_store_dir()
    if _is_real_agent_dir_under_pytest(store):
        _refuse_real_home_under_pytest(store)
        raise PermissionError(f"refusing to write real skill store from a test: {store}")

    payload = _pkg_skill_md(skill_src)
    pkg_hash = _sha256_bytes(payload)
    pkg_version = __version__

    try:
        _ensure_real_dir(store.parent)
    except FileNotFoundError:
        store.parent.mkdir(mode=0o700, parents=True)
        _ensure_real_dir(store.parent)

    with _store_publish_lock(store):
        _prepare_store_dir(store)
        _reject_payload_symlink(store)
        publish, notice = _should_publish(pkg_version, pkg_hash, store)
        if not publish:
            if not _store_is_healthy(store):
                raise FileNotFoundError(f"skill store payload missing at {store}")
            return store
        if notice:
            sys.stderr.write(notice)
        atomic_write_bytes(store / _STORE_PAYLOAD, payload, mode=0o644)
        meta = {
            "schema": 1,
            "skill_version": pkg_version,
            "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "published_by": str(skill_src),
            "min_mm_version": pkg_version,
        }
        atomic_write_bytes(
            store / _STORE_META,
            json.dumps(meta, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            mode=0o644,
        )
    if not _store_is_healthy(store):
        raise FileNotFoundError(f"publish left empty skill store at {store}")
    return store


def _legacy_shape(resolved: Path) -> Literal["package", "checkout", "other"]:
    parts = resolved.parts
    if len(parts) < 4 or parts[-3:] != _LEGACY_TAIL:
        return "other"
    if "site-packages" in parts or "dist-packages" in parts:
        return "package"
    if parts[-4] == "src":
        return "checkout"
    return "other"


def _points_at_store(target: Path, store: Path) -> bool:
    try:
        raw = os.readlink(target)
    except OSError:
        return False
    linked = Path(raw)
    return linked == store or linked == Path(str(store))


def _symlink_lives(target: Path) -> bool:
    try:
        target.resolve(strict=True)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise


def _replace_symlink(target: Path, store: Path) -> None:
    """Point ``target`` at ``store`` via tmp symlink + os.replace. Never unlink."""
    tmp = target.with_name(f".{target.name}.mm-new")
    try:
        if tmp.exists() or tmp.is_symlink():
            os.unlink(tmp)
        os.symlink(str(store), tmp)
        current = os.readlink(target) if target.is_symlink() else None
        if current is not None:
            still = os.readlink(target)
            if still != current:
                os.unlink(tmp)
                raise FileExistsError("skill link changed during replace")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def render_skill_status(result: SkillInstallResult) -> str:
    """Pure leaf: cause + readlink + copy-pasteable fix. Peer strings go through safe_str."""
    target = safe_str(str(result.target))
    readlink = ""
    try:
        if result.target.is_symlink():
            readlink = safe_str(os.readlink(result.target))
    except OSError:
        readlink = ""
    store = safe_str(str(result.link_target or _skill_store_dir()))
    if result.status == "dangling-ours":
        return (
            f"{target} is mm's symlink to {readlink or store} but the store is missing; "
            f"run: mm install-skills"
        )
    if result.status == "dangling-ours-legacy":
        return (
            f"{target} is a dangling mm symlink to {readlink or '(unreadable)'}; "
            f"run: mm install-skills"
        )
    if result.status == "foreign":
        dest = readlink or "a non-symlink"
        return (
            f"{target} exists and points at {dest}, which is not mm's skill store "
            f"({store}); move it aside and run: mm install-skills"
        )
    if result.status == "failed":
        return f"{target} installation failed: {safe_str(result.reason or 'unknown')}"
    if result.status == "installed":
        return f"{target} -> {store}"
    if result.status == "unchanged":
        return f"{target} -> {store}"
    return f"{target}: {result.status}"


def _ensure_retro_skill_link(*, dry_run: bool = False) -> SkillInstallResult | None:
    """Compatibility adapter for Claude Code's descriptor-driven installer."""
    return _ensure_skill_target(_skill_target_descriptors()[0], dry_run=dry_run)


def _ensure_codex_retro_skill_link(*, dry_run: bool = False) -> SkillInstallResult | None:
    """Compatibility adapter for Codex's descriptor-driven installer."""
    return _ensure_skill_target(_skill_target_descriptors()[1], dry_run=dry_run)


def _ensure_opencode_retro_skill_link(*, dry_run: bool = False) -> SkillInstallResult | None:
    """Compatibility adapter for OpenCode's descriptor-driven installer."""
    return _ensure_skill_target(_skill_target_descriptors()[2], dry_run=dry_run)


def _ensure_retro_skill_links(
    *,
    dry_run: bool = False,
    allow_mutate: bool = True,
    explicit: bool = False,
) -> tuple[SkillInstallResult, ...]:
    """Best-effort install for all three supported agent roots.

    Results are complete and ordered Claude Code, Codex, OpenCode. Expected
    filesystem failures are values, never exceptions, so init and push keep
    their best-effort contract while ``mm install-skills`` can report truthfully.

    ``dry_run=True`` returns full classifications with zero writes.
    ``allow_mutate=False`` (quiet/autopush) classifies and notices but never
    rewrites agent config.
    ``explicit=True`` (``mm install-skills`` / ``mm init``) will re-point a
    *live* checkout-shaped dogfood link; push will not.
    """
    write = bool(allow_mutate and not dry_run)
    descriptors = _skill_target_descriptors()
    results: list[SkillInstallResult | None] = [None] * len(descriptors)
    available: list[tuple[int, SkillTarget]] = []
    store = _skill_store_dir()

    if write and _is_real_agent_dir_under_pytest(store):
        _refuse_real_home_under_pytest(store)
        write = False

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

    if not available:
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

    skill_src: Path | None = None
    if write:
        try:
            skill_src = _resolve_retro_skill_source_once()
        except Exception as error:
            if _store_is_healthy(store):
                skill_src = None
            else:
                for index, descriptor in available:
                    results[index] = _failed_result(descriptor, "skill source resolution", error)
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

    published = _store_is_healthy(store)
    if write and skill_src is not None:
        try:
            store = _publish_skill_store(skill_src)
            published = _store_is_healthy(store)
        except (OSError, StorageError, PermissionError, FileNotFoundError) as error:
            for index, descriptor in available:
                results[index] = _failed_result(descriptor, "skill store publish", error)
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
        except Exception as error:
            for index, descriptor in available:
                results[index] = _failed_result(descriptor, "skill store publish", error)
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

    if write and not published:
        err = FileNotFoundError(f"skill store is empty at {store}")
        for index, descriptor in available:
            results[index] = _failed_result(descriptor, "skill store publish", err)
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

    for index, descriptor in available:
        try:
            results[index] = _install_available_skill_target(
                descriptor,
                skill_src,
                store=store,
                write=write,
                explicit=explicit,
            )
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
    descriptor: SkillTarget,
    *,
    dry_run: bool = False,
    allow_mutate: bool = True,
    explicit: bool = False,
) -> SkillInstallResult | None:
    """Run the legacy one-agent adapter without weakening its safety rules."""
    write = bool(allow_mutate and not dry_run)
    if _is_real_agent_dir_under_pytest(descriptor.target):
        _refuse_real_home_under_pytest(descriptor.target)
        if dry_run:
            return SkillInstallResult(
                descriptor,
                "failed",
                reason="refused to write a real agent directory from a test",
            )
        return None

    try:
        availability, failure = _agent_root_availability(descriptor)
    except Exception as error:
        return _failed_result(descriptor, "availability check", error)
    if availability == "unavailable":
        return SkillInstallResult(descriptor, "unavailable")
    if availability == "failed":
        return _failed_result(descriptor, "availability check", failure)

    store = _skill_store_dir()
    if write and _is_real_agent_dir_under_pytest(store):
        _refuse_real_home_under_pytest(store)
        return None

    skill_src: Path | None
    try:
        skill_src = _resolve_retro_skill_source_once()
    except Exception as error:
        if not _store_is_healthy(store):
            return _failed_result(descriptor, "skill source resolution", error)
        skill_src = None

    if write and skill_src is not None:
        try:
            store = _publish_skill_store(skill_src)
        except (OSError, StorageError, PermissionError, FileNotFoundError) as error:
            return _failed_result(descriptor, "skill store publish", error)
        if not _store_is_healthy(store):
            return _failed_result(
                descriptor,
                "skill store publish",
                FileNotFoundError(f"skill store is empty at {store}"),
            )

    return _install_available_skill_target(
        descriptor,
        skill_src,
        store=store,
        write=write,
        explicit=explicit,
    )


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


def _install_available_skill_target(
    descriptor: SkillTarget,
    skill_src: Path | None,
    *,
    store: Path,
    write: bool,
    explicit: bool,
) -> SkillInstallResult:
    """Install or classify one link after the agent root is known good.

    Link step is gated on a healthy store when ``write`` is True. Never unlinks.
    """
    target = descriptor.target
    try:
        skills_info = descriptor.skills_dir.stat()
    except FileNotFoundError:
        if write:
            try:
                descriptor.skills_dir.mkdir(mode=0o700)
            except OSError as error:
                return _failed_result(descriptor, "skills directory setup", error)
        else:
            return SkillInstallResult(descriptor, "unavailable", skill_src=skill_src)
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

    if target_info is not None and not stat.S_ISLNK(target_info.st_mode):
        _emit_conflict_notice(target, conflict_marker=descriptor.conflict_marker)
        return SkillInstallResult(descriptor, "foreign", skill_src=skill_src, link_target=store)

    if target_info is not None and stat.S_ISLNK(target_info.st_mode):
        try:
            lives = _symlink_lives(target)
        except OSError as error:
            return _failed_result(descriptor, "target resolution", error)
        if _points_at_store(target, store):
            if lives:
                if write:
                    _touch_marker(descriptor.success_marker)
                return SkillInstallResult(
                    descriptor, "unchanged", skill_src=skill_src, link_target=store
                )
            status: SkillInstallStatus = "dangling-ours"
            if write:
                try:
                    _replace_symlink(target, store)
                except OSError as error:
                    return _failed_result(descriptor, "link repair", error)
                _touch_marker(descriptor.success_marker)
                return SkillInstallResult(
                    descriptor,
                    "installed",
                    skill_src=skill_src,
                    link_target=store,
                    reason="repaired dangling store link",
                )
            _emit_conflict_notice(target, conflict_marker=descriptor.conflict_marker)
            return SkillInstallResult(descriptor, status, skill_src=skill_src, link_target=store)

        resolved = target.resolve(strict=False)
        shape = _legacy_shape(resolved)
        if shape == "other":
            _emit_conflict_notice(target, conflict_marker=descriptor.conflict_marker)
            return SkillInstallResult(descriptor, "foreign", skill_src=skill_src, link_target=store)
        if lives and shape == "checkout" and not explicit:
            sys.stderr.write(
                f"mm: notice: {safe_str(str(target))} is a live checkout skill link "
                f"({safe_str(os.readlink(target))}); leaving it alone. "
                f"Run mm install-skills to point it at {safe_str(str(store))}.\n"
            )
            if write:
                _touch_marker(descriptor.success_marker)
            return SkillInstallResult(
                descriptor,
                "unchanged",
                skill_src=skill_src,
                link_target=store,
                reason="live-checkout",
            )
        if not lives:
            status = "dangling-ours-legacy"
            if write:
                try:
                    _replace_symlink(target, store)
                except OSError as error:
                    return _failed_result(descriptor, "link repair", error)
                _touch_marker(descriptor.success_marker)
                return SkillInstallResult(
                    descriptor,
                    "installed",
                    skill_src=skill_src,
                    link_target=store,
                    reason="repaired dangling-ours-legacy",
                )
            _emit_conflict_notice(target, conflict_marker=descriptor.conflict_marker)
            return SkillInstallResult(descriptor, status, skill_src=skill_src, link_target=store)
        # live package, or explicit live checkout: migrate
        if write:
            try:
                _replace_symlink(target, store)
            except OSError as error:
                return _failed_result(descriptor, "link migrate", error)
            _touch_marker(descriptor.success_marker)
            return SkillInstallResult(
                descriptor,
                "installed",
                skill_src=skill_src,
                link_target=store,
                reason="migrated",
            )
        return SkillInstallResult(
            descriptor,
            "unchanged",
            skill_src=skill_src,
            link_target=store,
            reason="would-migrate",
        )

    if not write:
        return SkillInstallResult(
            descriptor,
            "unavailable",
            skill_src=skill_src,
            link_target=store,
            reason="would-install",
        )
    try:
        os.symlink(str(store), target)
    except OSError as error:
        return _failed_result(descriptor, "link install", error)
    _touch_marker(descriptor.success_marker)
    return SkillInstallResult(descriptor, "installed", skill_src=skill_src, link_target=store)


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
    dest = ""
    try:
        if target.is_symlink():
            dest = os.readlink(target)
    except OSError:
        dest = ""
    where = f" -> {safe_str(dest)}" if dest else ""
    sys.stderr.write(
        f"mm: notice: skill at {safe_str(str(target))}{where} is not mm's store link; "
        f"not replacing (move it aside and run: mm install-skills)\n"
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


def _store_needs_refresh() -> bool:
    """True when the store is missing or its version/size disagrees with the package."""
    store = _skill_store_dir()
    payload = store / _STORE_PAYLOAD
    try:
        store_size = payload.stat().st_size
        if store_size <= 0:
            return True
    except OSError:
        return True
    try:
        skill_src = _resolve_retro_skill_src()
        pkg = skill_src / _STORE_PAYLOAD
        pkg_size = pkg.stat().st_size
    except Exception:
        return False
    meta = _read_store_meta(store)
    store_ver = str(meta.get("skill_version", "")) if meta else ""
    if store_ver != __version__ or store_size != pkg_size:
        return True
    return False


def _skill_link_check_due_at(target: Path, *, success_marker: str) -> bool:
    """Target-specific implementation of the 24-hour skill-link drift gate."""
    if not _marker_is_fresh(success_marker):
        return True
    try:
        target_info = target.lstat()
        if not stat.S_ISLNK(target_info.st_mode):
            return True
        store = _skill_store_dir()
        if not _points_at_store(target, store):
            return True
        if not target.exists():
            return True
        if _store_needs_refresh():
            return True
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


def diagnose_skill_links() -> list[dict[str, str]]:
    """Passphrase-free snapshot of the three agent links plus the store. No writes."""
    store = _skill_store_dir()
    rows: list[dict[str, str]] = []
    payload = store / _STORE_PAYLOAD
    store_state = "missing"
    try:
        if payload.is_file() and payload.stat().st_size > 0:
            store_state = "ok"
        elif payload.is_symlink():
            store_state = "symlink"
        elif payload.exists():
            store_state = "empty"
    except OSError as error:
        store_state = type(error).__name__
    meta = _read_store_meta(store)
    for descriptor in _skill_target_descriptors():
        row = {
            "agent": descriptor.display_name,
            "target": str(descriptor.target),
            "store": str(store),
            "store_state": store_state,
            "store_version": str(meta.get("skill_version", "")) if meta else "",
        }
        try:
            info = descriptor.target.lstat()
        except FileNotFoundError:
            row["status"] = "absent"
            rows.append(row)
            continue
        except OSError as error:
            row["status"] = "error"
            row["detail"] = _reason(error)
            rows.append(row)
            continue
        if not stat.S_ISLNK(info.st_mode):
            row["status"] = "foreign"
            row["detail"] = "not a symlink"
            rows.append(row)
            continue
        try:
            row["readlink"] = os.readlink(descriptor.target)
        except OSError as error:
            row["status"] = "error"
            row["detail"] = _reason(error)
            rows.append(row)
            continue
        if _points_at_store(descriptor.target, store):
            row["status"] = "ok" if descriptor.target.exists() else "dangling-ours"
        else:
            try:
                lives = _symlink_lives(descriptor.target)
            except OSError:
                lives = False
            shape = _legacy_shape(descriptor.target.resolve(strict=False))
            if not lives and shape in ("package", "checkout"):
                row["status"] = "dangling-ours-legacy"
            elif lives and shape == "checkout":
                row["status"] = "live-checkout"
            else:
                row["status"] = "foreign"
        rows.append(row)
    return rows


__all__ = [
    "SKILL_LINK_TTL_SECONDS",
    "SKILL_ROOTS",
    "SkillInstallResult",
    "SkillTarget",
    "_refuse_real_home_under_pytest",
    "diagnose_skill_links",
    "render_skill_status",
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
    "_skill_store_dir",
]
