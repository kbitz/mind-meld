"""Atomic file-write, directory-flush and flock-append primitives.

Atomic writes publish at successful os.replace. Before that boundary this
invocation preserves the target; afterward, with fsync=True, a parent-directory
error can leave complete new bytes visible with durability unconfirmed. Failed
return does not mean rollback. Owned-temp cleanup for caught OSError is
best-effort.

With fsync=True, normal return confirms file and parent flushes as reported
by the platform. Darwin prefers F_FULLFSYNC, falling back to os.fsync on
unsupported operations; other platforms use os.fsync. This does not certify
physical power-loss survival, iCloud upload, peer visibility or freedom from
cloud conflicts. fsync=False provides replacement visibility without fsync calls.

See docs/invariants/sync.md#atomic-write-publication-failures for the
"Atomic write publication failures" contract and caller recovery audit.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pwd
import stat
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path

from mind_meld.errors import StorageError

_IS_DARWIN = sys.platform == "darwin"


class PytestRealStateWriteError(BaseException):
    """A test tried to mutate account state; best-effort callers must not hide it."""


def _existing_ancestors(path: Path) -> Iterable[tuple[Path, os.stat_result]]:
    """Read identities through a missing suffix, without suppressing lookup errors.

    Nothing exists beneath a regular file, so ENOTDIR counts as missing and the
    helper keeps its native error.
    """
    for ancestor in (path, *path.parents):
        try:
            yield ancestor, ancestor.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue


def _root_identity(root: Path) -> tuple[os.stat_result, tuple[str, ...]]:
    """The root's deepest existing ancestor identity and the parts missing beneath it."""
    anchor, info = next(iter(_existing_ancestors(root)))
    return info, root.relative_to(anchor).parts


def _root_cache_identity(root: Path) -> tuple[int, int, int, int, tuple[str, ...]] | None:
    """Live identity keying cached root resolutions, or None when they must not be cached.

    A dangling symlink at the first missing component, or a ``..`` after one,
    resolves through links outside its anchor that can be retargeted without
    changing the anchor.
    """
    info, missing = _root_identity(root)
    if missing and (
        ".." in missing or os.path.lexists(root.parents[len(missing) - 1] / missing[0])
    ):
        return None
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, missing


def _aliases_protected_root(
    path: Path,
    ancestors: Sequence[tuple[Path, os.stat_result]],
    info: os.stat_result,
    missing: tuple[str, ...],
) -> bool:
    """Whether `path` reaches the root anchor `info` by any spelling of `missing`.

    `ancestors` is `path`'s existing-ancestor walk, read once per candidate.
    The deepest ancestor sharing the anchor's identity decides: shallower
    matches would need a directory loop.
    """
    for ancestor, candidate_info in ancestors:
        if (info.st_dev, info.st_ino) != (candidate_info.st_dev, candidate_info.st_ino):
            continue
        suffix = path.relative_to(ancestor).parts[: len(missing)]
        if tuple(part.casefold() for part in suffix) != tuple(part.casefold() for part in missing):
            return False
        # An existing differently spelled component beneath a missing canonical
        # one proves a distinct path on a sensitive filesystem. Missing aliases
        # are ambiguous: refuse rather than create a filesystem case probe.
        actual = ancestor
        for expected_part, actual_part in zip(missing, suffix):
            actual /= actual_part
            if actual_part != expected_part:
                try:
                    actual.stat()
                except (FileNotFoundError, NotADirectoryError):
                    pass
                else:
                    return False
        return True
    return False


def _mutated_entries(path: Path) -> list[Path]:
    """The destination plus each prefix a recursive mkdir can create before `..`.

    Normalizing first would hide `<root>/missing/../../x`, whose
    mkdir(parents=True) still creates `<root>/missing`.
    """
    absolute = path if path.is_absolute() else Path.cwd() / path
    entries = [absolute]
    prefix = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        if part == ".." and prefix.name not in ("", ".."):
            entries.append(prefix)
        prefix /= part
    return entries


# One MiB is generous for a hand-authored CLI config, while bounding test-only reads.
_PYTEST_CONFIG_MAX_BYTES = 1024 * 1024
_PYTEST_RECOVERY = "No write occurred. Fix the destination/config and rerun; nothing to clean up."


def _pytest_config_stamp(home: Path) -> tuple[Path, int, int, Path]:
    path = home / ".config/mind-meld/config.toml"
    try:
        info = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return path, -1, -1, home
    except OSError as exc:
        raise ValueError(
            f"real config {str(path)!r} is unreadable; "
            "restore read/search permissions on that file and its parents"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(
            f"real config {str(path)!r} is not a regular file; "
            "replace it with a regular TOML config"
        )
    if info.st_size > _PYTEST_CONFIG_MAX_BYTES:
        raise ValueError(
            f"real config {str(path)!r} exceeds _PYTEST_CONFIG_MAX_BYTES "
            f"({_PYTEST_CONFIG_MAX_BYTES}); reduce that config below the cap"
        )
    return path, info.st_mtime_ns, info.st_size, home


@lru_cache(maxsize=16)
def _pytest_config_strings(stamp: tuple[Path, int, int, Path]) -> tuple[tuple[str, str], ...]:
    """Cache raw path strings only; no settings values or expanded paths survive."""
    path, _, size, _ = stamp
    if size == -1:
        return ()
    try:
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("not a regular file")
            raw = stream.read(_PYTEST_CONFIG_MAX_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"real config {str(path)!r} is unreadable; "
            "restore a readable regular TOML file and its parent permissions"
        ) from exc
    if len(raw) > _PYTEST_CONFIG_MAX_BYTES:
        raise ValueError(
            f"real config {str(path)!r} exceeds _PYTEST_CONFIG_MAX_BYTES; "
            "reduce that config below the cap"
        )
    # Deliberately lazy: the production guard's inactive branch never imports TOML.
    import tomllib

    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError(
            f"real config {str(path)!r} has invalid TOML; "
            "fix its TOML syntax, encoding, duplicate keys or excessive nesting"
        ) from exc

    def malformed(key: str, edit: str) -> ValueError:
        return ValueError(f"real config {str(path)!r}: {key!r} {edit}")

    for key in ("storage", "sync"):
        if key in config and not isinstance(config[key], dict):
            raise malformed(key, "must be a table; replace the scalar with a TOML table")
    roots = []
    storage = config.get("storage", {}).get("path")
    if isinstance(storage, str) and storage:
        roots.append(("storage.path", storage))
    sync = config.get("sync", {})
    if "claude_dir" in sync:
        legacy = sync["claude_dir"]
        if not isinstance(legacy, str):
            raise malformed(
                "sync.claude_dir", "must be a path string; fix or remove the legacy key"
            )
        roots.append(("sync.claude_dir", legacy))
    if "sources" in sync:
        if not isinstance(sync["sources"], list):
            raise malformed("sync.sources", "must be a list; use [[sync.sources]] tables")
        for index, source in enumerate(sync["sources"]):
            key = f"sync.sources[{index}].path"
            if not isinstance(source, dict) or not isinstance(source.get("path"), str):
                raise malformed(key, "must be a path string; add or fix that source's path")
            roots.append((key, source["path"]))
    return tuple(roots)


def _expand_pytest_root(raw: str, home: Path) -> Path:
    """Expand path spellings without consulting HOME (unlike production expanduser)."""
    raw = str(Path(raw))  # Normalize like production first: "~//x" means "~/x".
    if raw == "~" or raw.startswith("~/"):
        path = home / raw[2:] if raw != "~" else home
    elif raw.startswith("~"):
        user, _, suffix = raw[1:].partition("/")
        user_home = pwd.getpwnam(user).pw_dir
        if not isinstance(user_home, str) or not Path(user_home).is_absolute():
            raise ValueError("named account home must be absolute")
        path = Path(user_home) / suffix
    else:
        path = Path(raw)
    return path if path.is_absolute() else Path.cwd() / path


def _pytest_refused_roots(
    home: Path,
) -> tuple[tuple[tuple[Path, str], ...], tuple[Path, int, int, Path]]:
    """Single refused-root inventory shared by the classifier and its pins.

    Raw strings cache by (real config path, mtime_ns, size, pwd home); expansion
    happens each call, so cwd and named-account changes cannot stale the roots.
    Disabled explicit sources remain real user data. Only path-typed values are
    retained or displayed. Config schema changes must update this extractor.
    """
    roots = [
        (home / suffix, suffix)
        for suffix in (
            ".config/mind-meld",
            "Library/Mobile Documents/com~apple~CloudDocs/mind-meld",
            "Sync/mind-meld",
            ".local/share/mind-meld",
            ".claude",
            ".codex",
            ".gstack",
            ".gstack-extend",
            ".cursor",
            ".local/share/muse",
            ".gemini/config",
            ".gemini/antigravity-cli",
        )
    ]
    stamp = _pytest_config_stamp(home)
    config_path = stamp[0]
    account = None
    for key, raw in _pytest_config_strings(stamp):
        try:
            root = _expand_pytest_root(raw, home)
        except Exception as exc:
            raise ValueError(
                f"real config {str(config_path)!r}: path expansion failed "
                f"for configured root {raw!r} from {key!r}; "
                "edit that path to an existing named account or fix its "
                "pwd lookup/current directory"
            ) from exc
        try:
            identity = _root_cache_identity(root)
            resolver = _pytest_resolved_roots
            if identity is None:  # uncacheable root; see _root_cache_identity
                resolver = _pytest_resolved_roots.__wrapped__
            resolved = resolver(((root, key),), stamp, (identity,))[0][2]
            if account is None:
                account = home.resolve()
        except Exception as exc:
            raise ValueError(
                f"real config {str(config_path)!r}: configured-root resolution failed "
                f"for {str(root)!r} from {key!r}; "
                "edit that path to fix its permissions, invalid spelling or symlink chain"
            ) from exc
        if account.is_relative_to(resolved):
            raise ValueError(
                f"real config {str(config_path)!r}: configured root {raw!r} "
                f"(resolved {str(resolved)!r}) "
                f"from {key!r} contains pwd account home {str(home)!r}; "
                "edit that path to a narrower destination below or outside the account home"
            ) from ValueError("configured root contains the account home")
        roots.append((root, key))
    return tuple(roots), stamp


@lru_cache(maxsize=32)
def _pytest_resolved_roots(
    roots: tuple[tuple[Path, str], ...],
    stamp: tuple[Path, int, int, Path],
    identities: tuple[object, ...],
) -> tuple[tuple[Path, str, Path], ...]:
    """Resolve roots; stamp and identities are cache keys only.

    Roots arrive absolute (relative config spellings re-expand against cwd per
    call), so their resolution needs no cwd key.

    Live identities invalidate creation, removal and retargeting of stable root
    symlinks. Callers bypass this cache (via __wrapped__) when _root_cache_identity
    returns None (a dangling or ``..``-bearing missing suffix).
    """
    resolved = []
    for root, key in roots:
        try:
            resolved.append((root, key, root.resolve()))
        except Exception as exc:
            raise ValueError(f"protected root {str(root)!r}") from exc
    return tuple(resolved)


@lru_cache(maxsize=32)
def _pytest_root_anchors(
    resolved_roots: tuple[tuple[Path, str, Path], ...],
    stamp: tuple[Path, int, int, Path],
    identities: tuple[object, ...],
) -> tuple[tuple[Path, str, os.stat_result, tuple[str, ...]], ...]:
    """Anchor identities for both spellings; same cache keys as _pytest_resolved_roots."""
    anchors = []
    for root, key, resolved in resolved_roots:
        try:
            for spelling in dict.fromkeys((root, resolved)):
                anchors.append((root, key, *_root_identity(spelling)))
        except Exception as exc:
            raise ValueError(f"protected root {str(root)!r}") from exc
    return tuple(anchors)


def refuse_real_state_write_under_pytest(path: Path) -> None:
    """Refuse inventoried writes to real account, agent and configured state.

    Silent outside pytest: an absent/empty marker returns before any account,
    path or config lookup, including importing tomllib. Independent of conftest.
    Classify lexical/resolved locations, unfollowed entries inside resolved
    parents and ancestor identities. Cached root resolutions/anchors key on the
    absolute roots, config stamp and live identities; raw config strings
    re-expand per call. This is an accident tripwire, not a malicious-test or symlink-race
    sandbox. See docs/invariants/README.md#test-write-guard.
    """
    test = os.environ.get("PYTEST_CURRENT_TEST")
    if not test:
        return

    def refusal(cause: str, remedy: str) -> PytestRealStateWriteError:
        return PytestRealStateWriteError(
            f"Test write guard refused {str(path)!r}; PYTEST_CURRENT_TEST={test!r}: "
            f"{cause}. {remedy}. {_PYTEST_RECOVERY} "
            "See docs/invariants/README.md#test-write-guard"
        )

    try:
        home_value = pwd.getpwuid(os.getuid()).pw_dir
        if not isinstance(home_value, str) or not home_value or "\x00" in home_value:
            raise ValueError("account home must be a nonempty absolute directory")
        home = Path(home_value)
        if not home.is_absolute() or not stat.S_ISDIR(home.stat().st_mode):
            raise ValueError("account home must be an absolute existing directory")
    except Exception as exc:
        raise refusal(
            "account-home lookup failed (pwd.getpwuid(os.getuid()).pw_dir)",
            "Repair the account-home lookup or its unusable directory; "
            "isolate destinations under tmp_path",
        ) from exc

    try:
        roots, stamp = _pytest_refused_roots(home)
    except Exception as exc:
        raise refusal(
            f"configured-root lookup failed: {exc}", "Repair the real config path described above"
        ) from exc

    def unclassifiable(subject: str) -> PytestRealStateWriteError:
        return refusal(
            f"path-resolution failed while classifying {subject}",
            "Repair the path lookup, permissions or symlink chain; "
            "isolate destinations under tmp_path",
        )

    def protected(root, key, alias=False):
        return refusal(
            f"protected account-state path{' or alias' if alias else ''} under {str(root)!r}; "
            f"matched root={str(root)!r}, source key={key!r}, real config={str(stamp[0])!r}, "
            f"pwd account home={str(home)!r}; $HOME is ignored",
            "Isolate write destinations under tmp_path and keep the isolation fixtures enabled",
        )

    try:
        entries = _mutated_entries(path)
        lexical = [Path(os.path.abspath(entry)) for entry in entries]
        # Parts-prefix containment: is_relative_to rebuilds every parent per root.
        lexical_roots = [(root, key, Path(os.path.abspath(root)).parts) for root, key in roots]
        for candidate in lexical:
            parts = candidate.parts
            for root, key, root_parts in lexical_roots:
                if parts[: len(root_parts)] == root_parts:
                    raise protected(root, key)
        candidates = dict.fromkeys(lexical)
        for entry in entries:
            candidates[entry.resolve()] = None
            if entry.name not in ("", ".."):
                candidates[entry.parent.resolve() / entry.name] = None
    except Exception as exc:
        raise unclassifiable("the write destination") from exc

    # Validate live anchor identities before cache reuse. Defer lookup failures
    # until after containment so an unrelated inaccessible root cannot mask a hit.
    identities = []
    for root, key in roots:
        try:
            identity = _root_cache_identity(root)
        except Exception:
            identity = None
        if identity is None:
            identities = None  # Resolve uncached; lookup failures surface below.
            break
        identities.append(identity)
    try:
        resolver = (
            _pytest_resolved_roots if identities is not None else _pytest_resolved_roots.__wrapped__
        )
        resolved_roots = resolver(roots, stamp, tuple(identities or ()))
    except Exception as exc:
        raise unclassifiable(str(exc)) from (exc.__cause__ or exc)
    candidate_parts = [candidate.parts for candidate in candidates]
    for root, key, resolved_root in resolved_roots:
        root_parts = resolved_root.parts
        if any(parts[: len(root_parts)] == root_parts for parts in candidate_parts):
            raise protected(root, key, alias=True)

    try:
        anchor_reader = (
            _pytest_root_anchors if identities is not None else _pytest_root_anchors.__wrapped__
        )
        anchors = anchor_reader(tuple(resolved_roots), stamp, tuple(identities or ()))
    except Exception as exc:
        raise unclassifiable(str(exc)) from (exc.__cause__ or exc)
    try:
        for candidate in candidates:
            ancestors = list(_existing_ancestors(candidate))
            for root, key, info, missing in anchors:
                if _aliases_protected_root(candidate, ancestors, info, missing):
                    raise protected(root, key, alias=True)
    except Exception as exc:
        raise unclassifiable("the write destination") from exc


def _default_new_file_mode() -> int:
    """Compute (0o666 & ~umask) — the mode Path.write_bytes() would use
    for a new file. Matches conventional Unix behavior so mm-written
    user-visible files (pull-applied session data, sync logs) inherit
    the same permissions as if the user's editor created them.
    """
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def _fsync_fd(fd: int) -> None:
    """Request the platform's flush for fd; return when it reports success.

    Darwin prefers F_FULLFSYNC to request flushing through the disk cache.
    ENOTSUP, EINVAL or EOPNOTSUPP falls back to os.fsync (including unsupported
    directory fds); non-Darwin uses os.fsync directly. Other F_FULLFSYNC errors
    and os.fsync errors raise OSError. The fallback does not promise the same
    physical-media guarantee as a supported F_FULLFSYNC.
    """
    if _IS_DARWIN:
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError as e:
            # Fall back only on "unsupported" errors. Real I/O errors propagate.
            if e.errno not in (errno.ENOTSUP, errno.EINVAL, errno.EOPNOTSUPP):
                raise
    os.fsync(fd)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    fsync: bool = False,
    mode: int | None = None,
) -> None:
    """Publish `data` at the `path` pathname via mkstemp + os.replace.

    Guarantees:
      - Successful replacement makes complete new bytes visible at `path`.
        Before it, this invocation leaves an existing target's bytes intact
        or an absent target absent. Concurrent external writers are not isolated.
      - With fsync=True, parent-directory open/flush/close runs after
        replacement and can fail with new bytes already published and
        durability unconfirmed. No rollback. With fsync=False no fallible
        step follows replacement, so OSError/StorageError from this call
        means it did not replace (asynchronous interrupts excepted).
      - Caught OSError triggers best-effort unlink of only the owned temp;
        successful replacement consumes that name. Cleanup is not guaranteed
        across process death, unlink failure or arbitrary uncaught exceptions.

    See docs/invariants/sync.md#atomic-write-publication-failures for caller
    handlers and next readers; exception types/messages do not identify phase.

    Args:
        path: pathname to replace, including a symlink itself, not its referent.
              The parent directory must already exist.
        data: bytes to write.
        fsync: if True, flush the temp file before replacement and the parent
               afterward. Normal return confirms these platform-reported local
               flushes, not cloud delivery or certified power-loss survival.
               Defaults to False: no fsync calls or crash-persistence guarantee.
        mode: explicit file permission bits (e.g., 0o600 for secrets,
              0o644 for user-visible text). If None (default):
                - If the target exists, preserves its stat-observed mode
                  (stat follows a symlink's referent).
                - If the target is new, uses (0o666 & ~umask), matching
                  Path.write_bytes() behavior.
              The default exists because `tempfile.mkstemp` creates
              tmp files with 0o600 unconditionally, and `os.replace`
              preserves the SOURCE mode — which would silently
              downgrade every user-visible file this helper writes.

    Raises:
        StorageError: wraps OSError inside the temp-write/replacement try,
            including directory-close errors after publication. StorageError
            from fsync_dir propagates directly; it is not an OSError subclass.
        OSError: mode lookup errors other than FileNotFoundError occur before
            that try and propagate raw. Other uncaught exceptions also escape.
    """
    refuse_real_state_write_under_pytest(path)
    parent = path.parent

    # Resolve effective mode BEFORE opening the tmp — if the target
    # exists and we're preserving, we need its mode captured here.
    if mode is None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = _default_new_file_mode()

    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            if fsync:
                f.flush()
                _fsync_fd(f.fileno())

        # chmod BEFORE the rename so the target atomically appears with
        # the correct mode (no window where a reader could see 0o600).
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        tmp_name = None  # consumed by replace; no longer ours to unlink

        if fsync:
            fsync_dir(parent)

    except OSError as e:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise StorageError(f"storage: atomic_write_bytes({path}) — {e}") from e


class AppendSizeLimit(ValueError):
    """The complete JSONL batch would cross the caller's file-size ceiling."""


def flock_append_jsonl(
    path: Path,
    lines: Iterable[bytes],
    *,
    mode: int = 0o600,
    strict: bool = False,
    max_bytes: int | None = None,
) -> None:
    """Append N JSONL rows to `path` atomically under fcntl.flock(LOCK_EX).

    Each element of `lines` is one JSON-encoded row WITHOUT a trailing newline;
    the helper appends a single `\\n` after each row and separates an
    unterminated prior row before appending. All N rows share one
    flock window — best-effort batching, NOT transactionality (a crash mid-batch
    leaves a prefix of the rows on disk).

    Contract:
      - Parent dir created with parents=True (mode 0o700) if missing.
      - File created with O_APPEND|O_EXCL|O_NOFOLLOW only when the batch will
        fit; existing paths are opened O_NOFOLLOW and must be regular files.
      - LOCK_EX is BLOCKING (no LOCK_NB / no retry budget). mm.lockfile already
        serializes push-vs-push at the higher layer; cross-process contention
        on this helper is rare in practice.
      - Best-effort: OSError swallowed silently. Callers are forensic logs
        (mm-events), not data integrity — a crashed FS or
        permission flip MUST NOT break the calling sync.
        Attended host captures opt into ``strict``: write errors and
        short appends raise OSError. Existing forensic callers are unchanged.
      - `max_bytes` checks the whole batch, including any separator, under
        flock; `AppendSizeLimit` reports a skipped append without changing
        bytes and without creating a missing file.
    """
    payload = b"".join(line + b"\n" for line in lines)
    if not payload:
        return  # nothing to write; avoid creating the file

    refuse_real_state_write_under_pytest(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW
        try:
            fd = os.open(str(path), flags, mode)
        except FileNotFoundError:
            if max_bytes is not None and len(payload) > max_bytes:
                raise AppendSizeLimit("JSONL batch exceeds the file-size ceiling")
            try:
                fd = os.open(str(path), flags | os.O_CREAT | os.O_EXCL, mode)
            except FileExistsError:
                fd = os.open(str(path), flags, mode)
        try:
            # Refuse a device or pipe before fchmod can rewrite its mode.
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "JSONL append target is not a regular file")
            try:
                os.fchmod(fd, mode)
            except OSError:
                pass  # fchmod can fail on some filesystems; perms are best-effort
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                start = os.fstat(fd).st_size
                if start and os.pread(fd, 1, start - 1) != b"\n":
                    payload = b"\n" + payload
                # Decide under the same flock as the append, including the
                # separator byte. A concurrent writer cannot invalidate it. A file
                # created above and still empty already passed the pre-check.
                if max_bytes is not None and start + len(payload) > max_bytes:
                    raise AppendSizeLimit("JSONL batch exceeds the file-size ceiling")

                def _restore_prefix() -> None:
                    if not strict:
                        return
                    try:
                        os.ftruncate(fd, start)
                    except OSError:
                        pass

                try:
                    written = os.write(fd, payload)
                except OSError:
                    _restore_prefix()
                    raise
                if strict and written != len(payload):
                    _restore_prefix()
                    raise OSError(f"short JSONL append: wrote {written} of {len(payload)} bytes")
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except OSError:
        if strict:
            raise
        return  # forensic aid only; never block the calling sync


def append_rotatable_jsonl(
    path: Path, line: bytes, *, max_bytes: int, mode: int = 0o600, attempts: int = 8
) -> None:
    """Durably append one JSONL row to a spool that a reader rotates by rename.

    ``flock_append_jsonl`` locks a file that is never renamed. Here a reader may
    rename the spool aside while this writer waits for the lock, so the row is
    written only once the flock confirms ``path`` still names the locked inode;
    otherwise the writer reopens. A row can therefore never land in a file the
    reader already took. Errors raise (``AppendSizeLimit`` for the ceiling):
    the caller holds the only copy of the row.
    """
    refuse_real_state_write_under_pytest(path)
    payload = line + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for _ in range(attempts):
        fd = os.open(str(path), os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, mode)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(errno.EINVAL, "spool is not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                current = os.lstat(path)
            except FileNotFoundError:
                continue  # rotated away; the next open creates a fresh spool
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                continue
            start = os.fstat(fd).st_size
            data = payload
            if start and os.pread(fd, 1, start - 1) != b"\n":
                data = b"\n" + payload  # isolate a torn earlier row
            if start + len(data) > max_bytes:
                raise AppendSizeLimit("spool exceeds its size ceiling")
            if os.write(fd, data) != len(data):
                os.ftruncate(fd, start)
                raise OSError("short spool append")
            _fsync_fd(fd)
            # Every append: an earlier writer may have died before binding the
            # spool's directory entry, and this caller is about to report success.
            fsync_dir(path.parent)
            return
        finally:
            os.close(fd)
    raise OSError(errno.EAGAIN, "spool kept rotating during the append")


def rotate_jsonl(
    path: Path,
    destination: Path,
    *,
    retry_intervals: Sequence[float] = (0.01, 0.05, 0.1),
    deadline: float | None = None,
) -> bool:
    """Rename a spool aside once no ``append_rotatable_jsonl`` holds it.

    Returns False when there is nothing to rotate or a writer still holds the
    lock after the short retries (rotate on a later pass). Non-blocking, so a
    stuck writer can never wedge the caller; no retry sleeps past ``deadline``
    (a ``time.monotonic()`` value).
    """
    refuse_real_state_write_under_pytest(path)
    refuse_real_state_write_under_pytest(destination)
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(errno.EINVAL, "spool is not a regular file")
        for delay in (0.0, *retry_intervals):
            if delay and deadline is not None and time.monotonic() + delay > deadline:
                return False
            time.sleep(delay)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                continue
        else:
            return False
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            return False
        os.rename(path, destination)
        fsync_dir(path.parent)
        return True
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    """Request a platform flush of directory entries for `path`.

    atomic_write_bytes(fsync=True) calls this after publishing; pull-apply
    also uses it at batch end. It does not flush file contents or undo any
    rename. Normal return confirms _fsync_fd's platform-reported flush,
    including its unsupported-F_FULLFSYNC fallback, not cloud delivery.
    See docs/invariants/sync.md#atomic-write-publication-failures.

    Raises:
        StorageError: wraps OSError from opening or flushing the directory.
            Recent renames may be visible with durability unconfirmed.
        OSError: closing the descriptor in finally can fail, including after
            a successful flush, or mask a flush error. atomic_write_bytes
            wraps this close error; neither type nor prefix proves rollback.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as e:
        raise StorageError(f"storage: fsync_dir open({path}) — {e}") from e
    try:
        _fsync_fd(fd)
    except OSError as e:
        raise StorageError(f"storage: fsync_dir fsync({path}) — {e}") from e
    finally:
        os.close(fd)
