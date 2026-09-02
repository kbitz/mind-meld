"""Retro-fleet skill installer: symlinks, the 24h drift gate, and its markers.

Extracted from ``cli.py`` in Track 16A. `mm` keeps a ``retro-fleet`` skill
symlinked into each supported agent's skills directory and self-heals the
link behind a 24-hour TTL gate on every push.

Add agents only to ``AGENT_ROWS``. Never add a parallel agent-name list.
``AGENT_ROWS`` is canonical; ``_TEST_SKILL_ROOT_OVERRIDES`` is empty in
production and exists only so tests can redirect paths.

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

import errno
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


@dataclass(frozen=True)
class AgentRow:
    """One supported agent's skill-link identity.

    ``key`` is lowercase. Skill-link keys (``AGENT_ROWS``) and usage-reader
    keys (``events_tail.HOST_READER_SOURCE_GATE``) are separate vocabularies;
    they currently overlap only on ``codex``. Claude's skill link is gated
    via ``consent_source="claude"``, not the host-reader map.

    ``skills_root`` is a ``~``-relative STRING, never a Path.
    ``expanduser()`` must run at CALL time, or every agent path freezes at
    the developer's real HOME at import and defeats every isolation fixture.
    That is the Track 15B invariant: ``config.CONFIG_DIR`` is
    ``Path.home() / ...`` frozen at import, and that is exactly the hazard
    Track 15B deleted two constants for.

    ``consent_source`` is the ``get_sources`` name that authorizes writing
    this row's skill link. Required, no default: a future row must state
    its policy. ``None`` would mean "ungated", which is the defect Track 25C
    removes. Do not add a second ``row.consent_source in enabled`` test
    anywhere else — ``consented_agent_keys`` is the one derivation.
    """

    key: str
    display_name: str
    skills_root: str
    success_marker: str
    conflict_marker: str
    consent_source: str


# Active supported agents. Result order is a documented contract
# (``_ensure_retro_skill_links`` docstring + ordered-list assertions).
# Removal is a deliberate retirement, not a reordering. Ordinary isolation
# never patches this; exactly one structural-extension test does, via
# ``redirect_skill_paths(..., extra_rows=...)``.
AGENT_ROWS: tuple[AgentRow, ...] = (
    AgentRow(
        key="claude",
        display_name="Claude Code",
        skills_root="~/.claude/skills",
        success_marker="skill-link-checked",
        conflict_marker="skill-link-conflict",
        consent_source="claude",
    ),
    AgentRow(
        key="codex",
        display_name="Codex",
        skills_root="~/.codex/skills",
        success_marker="codex-skill-link-checked",
        conflict_marker="codex-skill-link-conflict",
        consent_source="codex",
    ),
)

# Empty in production. Tests patch this (together with AGENT_ROWS) so
# descriptors resolve under tmp_path. Never derive the real-home guard
# from this map.
_TEST_SKILL_ROOT_OVERRIDES: dict[str, str] = {}

# Bound to the row values so nothing that imported the old module-level
# names breaks. A rename of any of these four literals silently resets
# every fleet machine's 24h TTL and re-emits one notice.
_SKILL_LINK_SUCCESS_MARKER = next(row.success_marker for row in AGENT_ROWS if row.key == "claude")
_SKILL_LINK_CONFLICT_MARKER = next(row.conflict_marker for row in AGENT_ROWS if row.key == "claude")
_CODEX_SKILL_LINK_SUCCESS_MARKER = next(
    row.success_marker for row in AGENT_ROWS if row.key == "codex"
)
_CODEX_SKILL_LINK_CONFLICT_MARKER = next(
    row.conflict_marker for row in AGENT_ROWS if row.key == "codex"
)


def _home_relative(skills_root: str) -> Path:
    """Strip a leading ``~/``. Fail closed on anything else.

    A root that is not ``~/``-relative yields an empty relative path, so
    ``_REAL_HOME / result`` is ``_REAL_HOME`` itself. The guard then
    over-matches rather than going blind. Never raise: this runs at
    descriptor-build time on the ``mm status`` / ``mm diag`` path.
    """
    if skills_root.startswith("~/"):
        return Path(skills_root[2:])
    return Path()


def _real_guard_paths() -> tuple[Path, ...]:
    """Canonical real-home paths the pytest guard must refuse.

    Derived from ``AGENT_ROWS`` (never from ``_TEST_SKILL_ROOT_OVERRIDES``)
    plus the explicit extras: the retired OpenCode skills dir, the marker
    dir, and the 24A skill store.
    Ordinary test isolation patches the override map; if this derived from
    that map the guard's target set would become the tmp paths and go
    blind to every real agent dir, with a green suite.
    """
    return (
        *(_REAL_HOME / _home_relative(row.skills_root) for row in AGENT_ROWS),
        # Retired but still guarded — the OpenCode link may still be on
        # disk until the one-shot reaper runs, and 67 measured tests once
        # wrote the developer's real agent dirs.
        _REAL_HOME / ".config" / "opencode" / "skills",
        _REAL_HOME / ".config" / "mind-meld",
        _REAL_HOME / ".local" / "share" / "mind-meld" / "agent-skills",
    )


def _is_real_agent_dir_under_pytest(target: Path) -> bool:
    """True when a TEST is about to write one of the real agent skills dirs.

    The installer mkdirs and symlinks into each ``AgentRow.skills_root``.
    ``conftest.py`` had eight autouse isolation fixtures and none covered
    these, so any test driving ``_push_core`` or ``init`` without stubbing
    the installer mutated the developer's real agent config dirs —
    **67 tests**, measured.

    Matches the registry's canonical agent paths plus the explicit
    extras, not "anything under ``$HOME``". A developer whose ``TMPDIR``
    lives under ``$HOME`` (common on macOS with a custom setting) would
    otherwise see every skill-link test fail with a message accusing the
    test of being unisolated when it is correctly using ``tmp_path``.

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
    return any(candidate == real or real in candidate.parents for real in _real_guard_paths())


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


SkillInstallStatus = Literal[
    "installed",
    "unchanged",
    "unavailable",
    "dangling-ours",
    "dangling-ours-legacy",
    "foreign",
    "failed",
    "declined",
    "removed-by-user",
]

# The `diagnose_skill_links` statuses that mean mm's own link is wedged and mm
# can act. `ok` / `live-checkout` (a deliberate dogfood link) / `foreign` (the
# user's own file) / `absent` (agent not installed) / `removed-by-user` (a link
# mm created and the user deleted) are all working-as-intended and must never be
# reported as broken. Consumed by `mm status`.
BROKEN_SKILL_STATUSES = (
    "dangling-ours",
    "dangling-ours-legacy",
    "foreign-dangling",
    "error",
)

_STORE_PAYLOAD = "SKILL.md"
_STORE_META = ".mm-skill.json"
_STORE_SENTINEL = ".mm-owned"
_STORE_LOCK = ".publish.lock"
_LEGACY_TAIL = ("mind_meld", "skills", "retro_fleet")


@dataclass(frozen=True)
class SkillTarget:
    """One supported agent's retro-fleet installation contract.

    Built at call time from ``AGENT_ROWS`` so patched roots and ``$HOME``
    take effect. ``key`` is the registry key.
    """

    key: str
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


_ORPHAN_OVERRIDE_WARNED: set[str] = set()


def _warn_orphan_overrides() -> None:
    """A key in the override map with no matching row is always a bug."""
    known = {row.key for row in AGENT_ROWS}
    for key in _TEST_SKILL_ROOT_OVERRIDES:
        if key in known or key in _ORPHAN_OVERRIDE_WARNED:
            continue
        _ORPHAN_OVERRIDE_WARNED.add(key)
        sys.stderr.write(
            f"mm: notice: _TEST_SKILL_ROOT_OVERRIDES has orphan key {key!r} "
            f"with no matching AGENT_ROWS row\n"
        )


def _root_for(row: AgentRow) -> str:
    return _TEST_SKILL_ROOT_OVERRIDES.get(row.key, row.skills_root)


def _descriptor_from_row(row: AgentRow) -> SkillTarget:
    skills_dir = Path(_root_for(row)).expanduser()
    return SkillTarget(
        key=row.key,
        display_name=row.display_name,
        agent_root=skills_dir.parent,
        skills_dir=skills_dir,
        success_marker=row.success_marker,
        conflict_marker=row.conflict_marker,
    )


def _descriptor_for(key: str) -> SkillTarget:
    """Return the call-time descriptor for ``key``.

    Error names the known keys so a typo is actionable.
    """
    for row in AGENT_ROWS:
        if row.key == key:
            return _descriptor_from_row(row)
    known = ", ".join(row.key for row in AGENT_ROWS)
    raise KeyError(f"unknown agent key {key!r}; known keys: {known}")


def _skill_target_descriptors() -> tuple[SkillTarget, ...]:
    """Return fresh descriptors so patched roots and ``$HOME`` take effect."""
    _warn_orphan_overrides()
    return tuple(_descriptor_from_row(row) for row in AGENT_ROWS)


def consented_agent_keys(config: dict | None, sources: list[dict]) -> frozenset[str]:
    """The one derivation for which agent skill links mm may create.

    Caller supplies a validated config (or None) and already-resolved
    sources. This helper imports nothing from ``config`` and does not
    call ``get_sources``. It catches nothing.

    * ``config is None`` — no consent context (fresh pipx, no config) →
      every registry key. Not an error.
    * ``[skills] maintain_links`` is false → empty, ``agents`` ignored.
    * ``agents`` present (key-absence, never a falsy check) → that list
      ∩ known keys. Unknown names are inert. A non-empty list whose
      intersection is empty emits one ``mm: notice:`` per process
      (``_EMPTY_AGENTS_NOTICE_EMITTED``) so a config that only names
      retired agents does not silently decline every link.
    * else → rows whose ``consent_source`` is in the passed-in source
      names. On a non-explicit config ``get_sources`` auto-detects by
      directory existence, so this is the same bit the host-usage read
      gate already uses, not ideal consent.
    """
    known = frozenset(row.key for row in AGENT_ROWS)
    if config is None:
        return known
    skills = config.get("skills")
    if not isinstance(skills, dict):
        skills = {}
    if not skills.get("maintain_links", True):
        return frozenset()
    if "agents" in skills:
        requested = skills["agents"]
        granted = frozenset(key for key in requested if key in known)
        if requested and not granted:
            global _EMPTY_AGENTS_NOTICE_EMITTED
            if not _EMPTY_AGENTS_NOTICE_EMITTED:
                _EMPTY_AGENTS_NOTICE_EMITTED = True
                sys.stderr.write(
                    "mm: notice: [skills] agents lists no currently supported "
                    "agent; every skill link is declined. Use maintain_links = "
                    "false to say none, or name a currently supported agent.\n"
                )
        return granted
    names = {src.get("name") for src in sources}
    return frozenset(row.key for row in AGENT_ROWS if row.consent_source in names)


def _row_is_consented(key: str, may_create: frozenset[str] | None) -> bool:
    """True when this row may be written.

    ``may_create is None`` means no consent context — allow all
    (fresh-machine allow-all). Callers of the writers must pass this
    explicitly; ``diagnose_skill_links`` maps ``None`` to unknown policy.
    """
    return may_create is None or key in may_create


def _declined_result(descriptor: SkillTarget) -> SkillInstallResult:
    return SkillInstallResult(
        descriptor,
        "declined",
        reason="skill-link maintenance is not enabled for this agent",
    )


def _finalize(
    descriptors: tuple[SkillTarget, ...],
    results: list[SkillInstallResult | None],
) -> tuple[SkillInstallResult, ...]:
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


def _reason(error: BaseException) -> str:
    """Return the safe, stable failure detail surfaced by the CLI."""
    return f"{type(error).__name__}: {safe_str(error)}"


_MM_SKILLS_DIR_REJECTION_EMITTED = False
_EMPTY_AGENTS_NOTICE_EMITTED = False


def _skill_store_dir() -> Path:
    """Return the mm-owned skill store. Call-time seam — never expanduser at import.

    ``MM_SKILLS_DIR`` is a test-only override, gated on ``PYTEST_CURRENT_TEST``.
    Set it outside a test and it is ignored, with one ``mm: error:`` to stderr.
    The default sits next to the mm-events root but outside every
    ``DEFAULT_SOURCES`` ``include_dirs`` entry.
    """
    global _MM_SKILLS_DIR_REJECTION_EMITTED
    override = os.environ.get("MM_SKILLS_DIR")
    if override:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return Path(override).expanduser()
        if not _MM_SKILLS_DIR_REJECTION_EMITTED:
            _MM_SKILLS_DIR_REJECTION_EMITTED = True
            sys.stderr.write("mm: error: MM_SKILLS_DIR is a test-only override; ignoring it\n")
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
    except (OSError, ValueError):
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
    # Ownership is the SENTINEL (and mm's own namespaced metadata) -- never the
    # payload. `SKILL.md` is the canonical Agent Skills filename, so a user who
    # hand-authored a retro-fleet skill here would otherwise have it silently
    # overwritten: a payload-only directory would read as "owned", the sentinel
    # would be planted, `_should_publish` would see `meta is None` and publish,
    # and their file would be gone with no backup and no notice.
    owned = _STORE_SENTINEL in names or _STORE_META in names
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
    except RuntimeError:
        # py3.11/3.12 raise RuntimeError("Symlink loop from ...") where 3.13+
        # raise OSError(ELOOP). Normalize both to "not live" so all supported
        # Pythons agree -- otherwise the same filesystem state is a repairable
        # classification on 3.11 and a hard `failed` on the 3.13 CI runs.
        return False
    except OSError as error:
        if error.errno == errno.ELOOP:
            return False
        raise


def _replace_symlink(target: Path, store: Path) -> None:
    """Point ``target`` at ``store`` via tmp symlink + os.replace.

    Never unlinks THE TARGET. It does unlink its own ``tmp`` scratch path.
    The distinction matters now that "mm does not remove your link" is a
    user-facing promise in the README.
    """
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
    store_path = result.link_target or _skill_store_dir()
    store = safe_str(str(store_path))
    restart = ", then restart the agent so it reloads SKILL.md"
    if result.status == "dangling-ours":
        return (
            f"{target} is mm's symlink to {readlink or store} but the store is missing; "
            f"run: mm install-skills{restart}"
        )
    if result.status == "dangling-ours-legacy":
        return (
            f"{target} is a dangling mm symlink to {readlink or '(unreadable)'}; "
            f"run: mm install-skills{restart}"
        )
    if result.status == "foreign":
        dest = readlink or "a non-symlink"
        return (
            f"{target} exists and points at {dest}, which is not mm's skill store "
            f"({store}); move it aside and run: mm install-skills{restart}"
        )
    if result.status == "failed":
        return f"{target} installation failed: {safe_str(result.reason or 'unknown')}"
    if result.status == "installed":
        return f"{target} -> {store}"
    if result.status == "unchanged":
        return f"{target} -> {store}"
    if result.status == "removed-by-user":
        return (
            f"{target} was removed and mm left it removed. Put it back: mm install-skills{restart}"
        )
    if result.status == "declined":
        reason = result.reason or "skill-link maintenance is off"
        remedy = f"mm install-skills --agent {result.descriptor.key}"
        surviving = ""
        try:
            if (
                result.target.is_symlink()
                and _points_at_store(result.target, store_path)
                and _symlink_lives(result.target)
                and _store_is_healthy(store_path)
            ):
                surviving = f" (an mm-owned link is still present at {target} and still works)"
        except OSError:
            surviving = ""
        return (
            f"{result.descriptor.display_name} — {reason}. "
            f"Keep the link maintained: {remedy}{surviving}"
        )
    return f"{target}: {result.status}"


def _reap_retired_opencode_skill_link() -> None:
    """One-shot, best-effort removal of the retro-fleet link mm created
    under ``~/.config/opencode/skills``.

    Ownership proof is ``os.readlink(target) == store``. A regular file, a
    directory, or a link pointing anywhere else is the user's and is left
    untouched. A dangling store-backed symlink is still ours and is
    unlinked — ``os.readlink`` does not care about liveness. ``lstat``,
    never ``resolve()`` — following the symlink defeats the check.
    Re-readlink immediately before ``unlink`` so a same-uid swap between
    the check and the delete is not applied. ``except OSError: pass``.
    Idempotent: later runs are no-ops once the link is gone. Do not add
    a persistent "did I reap it" marker.

    This does not conflict with Track 28A. That guard says an ABSENT link
    is user intent and must not be recreated. It says nothing about mm
    removing a link it created and no longer maintains.
    """
    target = Path("~/.config/opencode/skills/retro-fleet").expanduser()
    if _is_real_agent_dir_under_pytest(target):
        return
    store = str(_skill_store_dir())
    try:
        st = os.lstat(target)
    except OSError:
        pass
    else:
        if stat.S_ISLNK(st.st_mode):
            try:
                if os.readlink(target) == store:
                    still = os.readlink(target)
                    if still == store:
                        os.unlink(target)
            except OSError:
                pass
    marker_dir = _marker_dir()
    for name in (
        "opencode-skill-link-checked",
        "opencode-skill-link-conflict",
    ):
        try:
            os.unlink(marker_dir / f".{name}")
        except OSError:
            pass


def _ensure_retro_skill_links(
    *,
    dry_run: bool = False,
    allow_mutate: bool = True,
    explicit: bool = False,
    may_create: frozenset[str] | None,
) -> tuple[SkillInstallResult, ...]:
    """Best-effort install for every ``AGENT_ROWS`` entry.

    Results are complete and ordered as ``AGENT_ROWS`` (active supported
    agents; removal is a deliberate retirement, not a reordering). Expected
    filesystem failures are
    values, never exceptions, so init and push keep their best-effort
    contract while ``mm install-skills`` can report truthfully.

    ``dry_run=True`` returns full classifications with zero writes.
    ``allow_mutate=False`` (quiet/autopush) classifies and notices but never
    rewrites agent config.
    ``explicit=True`` (``mm install-skills`` / ``mm init``) will re-point a
    *live* checkout-shaped dogfood link; push will not. It ALSO bypasses the
    removed-by-user guard, which makes it the single documented way to undo a
    deletion (README "Removing a skill link").
    ``may_create`` is required (no default): a forgotten kwarg is a
    ``TypeError`` on the first test run instead of silently authorising
    every row. ``None`` still means no consent context (allow all) —
    fresh-machine allow-all is documented intent. A declined row is
    classified before any ``stat`` on that agent root, reaches neither
    ``_failed_result`` nor ``_emit_status_notice``, and does not touch
    its success marker.

    Store publish is never gated on agent consent: if the mm-owned store
    already exists it is refreshed even when every row is declined. An
    absent store is created only when at least one row is consented.
    """
    write = bool(allow_mutate and not dry_run)
    if write:
        # One-shot retirement of the OpenCode skill link mm created.
        # This function already runs behind the 24h drift gate on push,
        # and mm init / mm install-skills also reach it. Not on mm status
        # or mm diag — those are read-only. Track 28A says an ABSENT link
        # is user intent and must not be recreated; this path is mm
        # removing a link it created and no longer maintains.
        _reap_retired_opencode_skill_link()
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
        if not _row_is_consented(descriptor.key, may_create):
            results[index] = _declined_result(descriptor)
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

    # Publish before the empty-available return when the owned store exists,
    # so an all-declined machine still refreshes a store its surviving links
    # point at. Creating a new store still requires at least one consented
    # available row.
    should_publish = write and (bool(available) or _owned_store_exists())
    if not available and not should_publish:
        return _finalize(descriptors, results)

    skill_src: Path | None = None
    if should_publish:
        try:
            skill_src = _resolve_retro_skill_source_once()
        except Exception as error:
            if _store_is_healthy(store):
                skill_src = None
            elif available:
                for index, descriptor in available:
                    results[index] = _failed_result(descriptor, "skill source resolution", error)
                return _finalize(descriptors, results)
            else:
                sys.stderr.write(f"mm: notice: skill store refresh failed: {_reason(error)}\n")
                return _finalize(descriptors, results)

    published = _store_is_healthy(store)
    if should_publish and skill_src is not None:
        try:
            store = _publish_skill_store(skill_src)
            published = _store_is_healthy(store)
        except (OSError, StorageError, PermissionError, FileNotFoundError) as error:
            if available:
                for index, descriptor in available:
                    results[index] = _failed_result(descriptor, "skill store publish", error)
                return _finalize(descriptors, results)
            sys.stderr.write(f"mm: notice: skill store refresh failed: {_reason(error)}\n")
            return _finalize(descriptors, results)
        except Exception as error:
            if available:
                for index, descriptor in available:
                    results[index] = _failed_result(descriptor, "skill store publish", error)
                return _finalize(descriptors, results)
            sys.stderr.write(f"mm: notice: skill store refresh failed: {_reason(error)}\n")
            return _finalize(descriptors, results)

    if not available:
        return _finalize(descriptors, results)

    if write and not published:
        err = FileNotFoundError(f"skill store is empty at {store}")
        for index, descriptor in available:
            results[index] = _failed_result(descriptor, "skill store publish", err)
        return _finalize(descriptors, results)

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

    return _finalize(descriptors, results)


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
        outcome = SkillInstallResult(descriptor, "foreign", skill_src=skill_src, link_target=store)
        _emit_status_notice(outcome, write=write)
        return outcome

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
            outcome = SkillInstallResult(descriptor, status, skill_src=skill_src, link_target=store)
            _emit_status_notice(outcome, write=write)
            return outcome

        resolved = target.resolve(strict=False)
        shape = _legacy_shape(resolved)
        if shape == "other":
            outcome = SkillInstallResult(
                descriptor, "foreign", skill_src=skill_src, link_target=store
            )
            _emit_status_notice(outcome, write=write)
            return outcome
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
            outcome = SkillInstallResult(descriptor, status, skill_src=skill_src, link_target=store)
            _emit_status_notice(outcome, write=write)
            return outcome
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

    # The target is absent. If mm has installed here before, the user removed
    # it -- deletion is intent, not damage, so push must not resurrect it.
    # `explicit=True` (`mm install-skills` / `mm init`) is the documented way
    # to put it back and deliberately skips this guard.
    if not explicit and _marker_exists(descriptor.success_marker):
        return SkillInstallResult(
            descriptor,
            "removed-by-user",
            skill_src=skill_src,
            link_target=store,
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


def _emit_status_notice(result: SkillInstallResult, *, write: bool) -> None:
    """Notice once per 24h — gated by the conflict marker.

    The text comes from ``render_skill_status``, so the push path and
    ``mm install-skills`` state the SAME cause. The previous single hardcoded
    string said "is not mm's store link" for every branch, including
    ``dangling-ours`` -- a link that byte-equals the store constant and is
    therefore provably mm's own -- and told the user to move it aside, which
    is the one action that would have prevented mm repairing it.

    ``write=False`` (dry-run / autopush classify-only) must NOT touch the
    marker: a run that will never repair anything would otherwise consume the
    24h notice budget, so the one run that COULD fix the link stays silent.
    """
    marker = result.descriptor.conflict_marker
    if _marker_is_fresh(marker):
        return
    sys.stderr.write(f"mm: notice: {render_skill_status(result)}\n")
    if write:
        _touch_marker(marker)


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


def _marker_exists(name: str) -> bool:
    """Return True iff the row's success marker is on disk, ignoring its age.

    Existence and freshness are separate questions. ``_marker_is_fresh``
    answers "should the drift gate re-check this row"; this answers "has mm
    ever resolved this target successfully". The marker is touched on every
    successful outcome including ``unchanged``.

    Precisely: it records "mm looked and was satisfied", NOT "mm created this
    link". The live-checkout branch touches it for a dogfood link mm
    deliberately refuses to own. So a deleted checkout link is also left
    deleted -- defensible (you deleted it) but it is not the "link mm created"
    proof an earlier draft of this docstring claimed.

    Fails OPEN (returns False) on an unreadable marker dir, matching
    ``_marker_is_fresh``. Fail-closed was the first instinct here -- if we
    cannot tell whether mm installed, do not resurrect -- but it trades a
    loud, recoverable outcome for a silent one: an unreadable marker dir
    would suppress self-heal forever with no message, which is the TODO#3
    bug ``_marker_is_fresh`` was fixed for and a violation of the
    visible-failure contract. A resurrected link is visible and the user can
    delete it again; silently never installing is not.

    Reads via ``Path.stat`` rather than ``Path.is_file``, deliberately and
    identically to ``_marker_is_fresh``. ``Path.is_file()`` calls ``os.stat``
    internally on current CPython (verified 3.14; CI runs 3.13), so it
    bypasses a ``Path.stat`` fault
    injection -- the two predicates would then disagree about the same
    unreadable marker, one failing open and one not. That symmetry is why this
    follows symlinks where the gate's target probe uses ``lstat``: a marker
    symlink is same-uid, same-trust-domain, and switching to ``lstat`` here
    would silently reintroduce the disagreement.
    """
    marker = _marker_dir() / f".{name}"
    try:
        marker.stat()
    except OSError:
        return False
    return True


def _touch_marker(name: str) -> None:
    """Mtime-touch the named marker. Best-effort; OSError is swallowed
    silently (the next push will simply re-run the installer)."""
    marker_dir = _marker_dir()
    # Guard the WRITE LOCATION, not just one caller. Markers live in
    # ~/.config/mind-meld, which conftest redirects — but a test that opts out
    # of that fixture, or any future path reaching here without going through
    # _ensure_retro_skill_links, would otherwise write the developer's real
    # config dir with the guard silent.
    if _is_real_agent_dir_under_pytest(marker_dir):
        _refuse_real_home_under_pytest(marker_dir)
        return
    try:
        marker_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (marker_dir / f".{name}").touch()
    except OSError:
        pass


def _marker_dir() -> Path:
    return Path("~/.config/mind-meld").expanduser()


def _skill_links_check_due(*, may_create: frozenset[str] | None) -> bool:
    """Return whether any supported agent's retro-fleet link has drifted.

    Gate consulted by ``_push_core``. Returns True when the installer
    should run. ``may_create`` MUST be the same frozenset the installer
    receives in the same push — a declined row never gets its success
    marker touched, so an unfiltered gate stays open forever.

    Three paths to True:

    1. **Owned store exists and needs refresh**, independent of any row's
       consent. Without this, an all-declined machine never reaches the
       installer and its surviving links freeze against a stale store.
    2. **Marker is stale** (or absent) — the original 24h-TTL behavior.
    3. **Marker is fresh but link state has drifted** — link is missing,
       dangling, or pointing somewhere other than our source.

    Declined rows are skipped before any ``stat``. Any I/O or resolver
    error in the drift check fails open (returns True) so the installer
    runs and emits its own notice.
    """
    if _owned_store_exists() and _store_needs_refresh():
        return True
    return any(
        _skill_link_check_due_for(descriptor, may_create=may_create)
        for descriptor in _skill_target_descriptors()
    )


def _skill_link_check_due_for(
    descriptor: SkillTarget, may_create: frozenset[str] | None = None
) -> bool:
    """Return whether one descriptor needs a repair attempt.

    A declined row is a quiet skip (zero I/O). A missing agent root is
    the only other quiet skip. A present root with no skills directory
    is immediately due, and every other inspection failure fails open
    so the plural installer can leave a forensic notice.
    """
    if not _row_is_consented(descriptor.key, may_create):
        return False
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


def _owned_store_exists() -> bool:
    """True when the mm-owned store directory is already on disk.

    Ownership is the ``.mm-owned`` sentinel or ``.mm-skill.json``, never
    the payload. A missing or foreign tree is False.
    """
    store = _skill_store_dir()
    try:
        names = {p.name for p in store.iterdir()}
    except OSError:
        return False
    return _STORE_SENTINEL in names or _STORE_META in names


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
    """Target-specific implementation of the 24-hour skill-link drift gate.

    ONE ``lstat``, and the absent case returns from inside its own handler.
    That shape is deliberate and load-bearing twice over.

    Correctness: an absent target means the user removed a link mm had
    resolved, and the gate must stay SHUT -- otherwise it returns True on
    every push forever for a row whose only possible outcome is a no-op (the
    installer declines to recreate, so it never touches the success marker,
    so the marker goes stale, so the gate stays hot). An earlier draft did
    this as a separate check placed above ``_marker_is_fresh``, which was
    correct but order-dependent: a ship-review mutant that moved it one line
    down broke the feature and passed 2741 tests. Returning from the
    ``FileNotFoundError`` handler makes that mutation unrepresentable.

    Cost: the earlier draft lstat'd the target here AND in the predicate
    above it, +1 syscall per consented row per push, forever. Measured at
    ship review. Store refresh is unaffected -- that path in
    ``_skill_links_check_due`` is independent of any row.
    """
    try:
        target_info = target.lstat()
    except FileNotFoundError:
        # Absent. A marker proves mm resolved this target before, so this is
        # a deliberate removal: stay shut. No marker means mm has never been
        # here (fresh machine) and the installer should run.
        return not _marker_exists(success_marker)
    except OSError:
        # Could not inspect. Not a removal -- fail open and let the installer
        # produce its own forensic outcome.
        return True
    if not _marker_is_fresh(success_marker):
        return True
    try:
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
    """The agent skill-link targets, in ``AGENT_ROWS`` order.

    Single source of truth for the target paths. ``install_skills_cmd`` used to
    rebuild this tuple from its own hardcoded string literals 3,000 lines away
    from the installer, so the two could drift; after the Track 16A cut they
    would have lived in different files under different Group 17 owners, and
    17A's charter is literally "installer correctness".

    Re-resolved per call (``expanduser`` reads ``$HOME`` at call time) so a test
    that moves HOME moves the targets with it.
    """
    return tuple(descriptor.target for descriptor in _skill_target_descriptors())


def _maintain_links_field(
    key: str,
    may_create: frozenset[str] | None,
    config_error: str | None,
) -> str:
    if config_error:
        return f"unknown (config invalid: {safe_str(config_error)})"
    if may_create is None:
        return "unknown (policy not resolved)"
    if _row_is_consented(key, may_create):
        return "enabled"
    return "disabled (not authorized by skill-link policy)"


def diagnose_skill_links(
    *,
    may_create: frozenset[str] | None = None,
    config_error: str | None = None,
) -> list[dict[str, str]]:
    """Passphrase-free snapshot of every agent link plus the store. No writes.

    Link ``status`` and ``maintain_links`` are orthogonal: a declined row
    can still be ``status: ok`` when an mm-owned link survives. Never fold
    policy into ``status``. When the config could not be parsed, the policy
    field is ``unknown (config invalid: …)``, never ``disabled``. A
    bare call (``may_create is None`` and no ``config_error``) is
    ``unknown (policy not resolved)`` — diagnosis must not assert a
    policy it never resolved.
    """
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
        try:
            rows.append(_diagnose_one(descriptor, store, store_state, meta))
        except Exception as error:  # never crash `mm status` / `mm diag`
            rows.append(
                {
                    "key": descriptor.key,
                    "agent": descriptor.display_name,
                    "target": str(descriptor.target),
                    "store": str(store),
                    "store_state": store_state,
                    "status": "error",
                    "detail": _reason(error),
                }
            )
    for row in rows:
        row["maintain_links"] = _maintain_links_field(row.get("key", ""), may_create, config_error)
    return rows


def _diagnose_one(
    descriptor: SkillTarget, store: Path, store_state: str, meta: dict | None
) -> dict[str, str]:
    """One descriptor's diagnose row. Raises; the caller turns that into `error`."""
    row = {
        "key": descriptor.key,
        "agent": descriptor.display_name,
        "target": str(descriptor.target),
        "store": str(store),
        "store_state": store_state,
        "store_version": str(meta.get("skill_version", "")) if meta else "",
    }
    try:
        info = descriptor.target.lstat()
    except FileNotFoundError:
        # `absent` means the agent has no link and mm never made one.
        # `removed-by-user` means mm made one and it is gone. Both are
        # working-as-intended, so neither is in BROKEN_SKILL_STATUSES -- but
        # only one of them is answerable with "run mm install-skills", and
        # collapsing them left `mm diag` unable to confirm a deliberate
        # deletion. This is link state, not policy, so it does not touch the
        # `maintain_links` field or its renderer contract.
        row["status"] = "removed-by-user" if _marker_exists(descriptor.success_marker) else "absent"
        return row
    except OSError as error:
        row["status"] = "error"
        row["detail"] = _reason(error)
        return row
    if not stat.S_ISLNK(info.st_mode):
        row["status"] = "foreign"
        row["detail"] = "not a symlink"
        return row
    try:
        row["readlink"] = os.readlink(descriptor.target)
    except OSError as error:
        row["status"] = "error"
        row["detail"] = _reason(error)
        return row
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
        elif not lives:
            # Dangling, but pointing somewhere mm does not recognize. Still
            # BROKEN from the agent's view -- it sees a dead skill entry --
            # even though mm must not touch it. Distinct from a live
            # `foreign` link, which is a working deliberate choice.
            row["status"] = "foreign-dangling"
        else:
            row["status"] = "foreign"
    return row


__all__ = [
    "AGENT_ROWS",
    "AgentRow",
    "BROKEN_SKILL_STATUSES",
    "SKILL_LINK_TTL_SECONDS",
    "SkillInstallResult",
    "SkillTarget",
    "consented_agent_keys",
    "diagnose_skill_links",
    "render_skill_status",
    "skill_targets",
    "_descriptor_for",
    "_ensure_retro_skill_links",
    "_marker_dir",
    "_marker_exists",
    "_real_guard_paths",
    "_refuse_real_home_under_pytest",
    "_resolve_retro_skill_src",
    "_skill_link_check_due_at",
    "_skill_links_check_due",
    "_skill_store_dir",
]
