"""Project identity for Claude project memory (Track 90A).

Claude keeps a project's memory in ``~/.claude/projects/<folder>/``, where
``<folder>`` is the checkout's absolute path with every non-alphanumeric
character replaced by ``-``. That name is machine-specific. This leaf maps a
folder to a machine-independent wire name:

* ``@git-<quote(identity)>`` when the folder is the checkout of a git project
  with a network ``origin`` (the identity is built from the canonical remote);
* ``@home`` / ``@home-<rest>`` when the folder is the encoded home or sits
  under it (the home-relative form);
* the literal folder name otherwise (machine-specific).

Nothing here depends on every machine sharing a layout: the checkout is found
by decoding each folder name against this machine's own filesystem. The index
is built lazily, once per process, with no git spawn on the common path, no
transcript read and no walk of the home folder. Leaf module: imports only
``gitenv``, ``safety`` and the standard library. See
docs/invariants/sync.md "Project identity on the wire".
"""

from __future__ import annotations

import configparser
import ipaddress
import os
import re
import socket
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, unquote, urlsplit

from mind_meld import gitenv, safety

# Wall-clock cap for building the index (measured ~60 ms for 361 folders).
INDEX_DEADLINE_SECONDS = 2.0
# A test opts a device leg into the codec; pytest alone keeps it off so that
# existing fixtures, and subprocesses that inherit PYTEST_CURRENT_TEST, behave
# as they did before the wire names.
WIRE_HOME_TEST_ENV = "MM_TEST_WIRE_HOME"

GIT_PREFIX = "@git-"
HOME_NAME = "@home"
OLD_PREFIX = "@old-"
LIT_PREFIX = "@lit-"
UNPLACEABLE_SKIP_REASON = "project name not placeable on this machine"

# Why a folder owns no identity (``rows[].why`` in ``mm diag --json``).
WHY_NO_ORIGIN = "no_origin"
WHY_LOCAL_REMOTE = "local_remote"
WHY_SUBMODULE = "submodule"
WHY_AMBIGUOUS = "ambiguous_decode"
WHY_NO_CHECKOUT = "no_checkout"
WHY_UNREADABLE = "unreadable"
WHY_TIMED_OUT = "timed_out"
WHY_SECOND_CLONE = "second_clone"
WHY_WORKTREE = "worktree"

# ---------------------------------------------------------------------------
# Remote canonicalization (moved from events.py, unchanged)
# ---------------------------------------------------------------------------

# scp-form: user@host:path (no scheme, NO `//` after the colon, NO `:port`).
# Examples: git@github.com:org/repo.git, hg@bitbucket.org:org/repo
_SCP_FORM = re.compile(r"^(?P<user>[^@:/\s]+)@(?P<host>[^:/\s]+):(?P<path>[^\s]+)$")


def canonicalize_remote_url(url: str) -> str:
    """Normalize a git remote URL to ``<host>/<path>`` form for fleet dedup.

    Strips: scheme, userinfo (user / user:password / token), port, query
    string, fragment, trailing ``.git``, leading slash. Lowercases the host
    while preserving path case. Returns "" on malformed input.

    Security (CT-10): credential-laden inputs MUST NOT survive the
    canonicalization. Tokens, x-access-token, OAuth user:password, and
    query-string ``?token=`` / ``?access_token=`` are all stripped.

    >>> canonicalize_remote_url("https://github.com/foo/bar.git")
    'github.com/foo/bar'
    >>> canonicalize_remote_url("git@github.com:foo/bar.git")
    'github.com/foo/bar'
    >>> canonicalize_remote_url("https://x-access-token:T@github.com/foo/bar.git?token=abc")
    'github.com/foo/bar'
    """
    if not isinstance(url, str):
        return ""
    s = url.strip()
    if not s:
        return ""

    # scp-form (no scheme) is special: urlsplit can't parse `git@host:path`
    # cleanly because it interprets `host:path` as `scheme:path`.
    m = _SCP_FORM.match(s)
    if m:
        host = m.group("host").lower()
        path = m.group("path").lstrip("/")
        return _strip_dot_git(f"{host}/{path}")

    # Has a scheme. Use urlsplit to peel off userinfo + port + query +
    # fragment. urlsplit needs a scheme to populate netloc correctly; if
    # the input is a bare host/path with no scheme, fall back to manual
    # parsing.
    if "://" not in s:
        # Bare `host/path` form — accept it as already-canonical-ish.
        return _strip_dot_git(s.lstrip("/"))

    parts = urlsplit(s)
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    path = (parts.path or "").lstrip("/")
    # urlsplit drops query and fragment automatically; just discard.
    return _strip_dot_git(f"{host}/{path}" if path else host)


def _strip_dot_git(s: str) -> str:
    if s.endswith(".git"):
        return s[: -len(".git")]
    if s.endswith("/"):
        return s.rstrip("/")
    return s


def _gitfile_target_dir(gitfile: Path) -> Path | None:
    """Resolve the ``gitdir:`` target of a ``.git`` file if it has a ``HEAD`` file."""
    try:
        raw = gitfile.read_bytes()
    except OSError:
        return None
    line = raw.split(b"\n", 1)[0]
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.startswith("gitdir: "):
        return None
    target = text[len("gitdir: ") :].strip()
    if not target:
        return None
    gitdir = Path(target)
    if not gitdir.is_absolute():
        gitdir = gitfile.parent / gitdir
    try:
        if gitdir.is_dir() and (gitdir / "HEAD").is_file():
            return gitdir
        return None
    except OSError:
        return None


def _resolve_git_dirs(path: Path) -> tuple[Path, Path] | None:
    """Return ``(gitdir, common_dir)`` for a git root, or ``None`` if not a valid root.

    Mirrors ``git rev-parse --git-dir --git-common-dir`` on the filesystem
    without spawning a subprocess:
    * For a normal clone, ``.git`` is a directory containing ``HEAD``; both
      ``gitdir`` and ``common_dir`` are ``(path / ".git").resolve()`` (unless
      a ``commondir`` file inside ``.git`` redirects ``common_dir``).
    * For a linked worktree or submodule, ``.git`` is a file whose first line
      is ``gitdir: <target>``. If ``<gitdir>/commondir`` exists (linked
      worktree), ``common_dir`` resolves ``commondir`` relative to ``gitdir``;
      otherwise (submodule) ``common_dir`` is ``gitdir``.
    """
    try:
        dot_git = path / ".git"
        if dot_git.is_dir():
            if not (dot_git / "HEAD").is_file():
                return None
            gitdir = dot_git.resolve()
        elif dot_git.is_file():
            gitdir_raw = _gitfile_target_dir(dot_git)
            if gitdir_raw is None:
                return None
            gitdir = gitdir_raw.resolve()
        else:
            return None
        commondir_file = gitdir / "commondir"
        if commondir_file.is_file():
            raw = commondir_file.read_bytes()
            rel = raw.split(b"\n", 1)[0].decode("utf-8").strip()
            if not rel:
                return None
            cd = Path(rel)
            if not cd.is_absolute():
                cd = gitdir / cd
            if not cd.is_dir() or not (cd / "HEAD").is_file():
                return None
            common_dir = cd.resolve()
        else:
            common_dir = gitdir
        return gitdir, common_dir
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _origin_remote_url(root: Path) -> str:
    """Return the `origin` remote URL or empty string on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=gitenv.scrubbed_git_env(),
            timeout=2,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

_DEFAULT_PORTS = {"https": 443, "http": 80, "git": 9418, "ssh": 22}
# scp-form with no user: `host:path`. git treats a colon before any slash this
# way; one letter before it is a drive, not a host, a second colon is a remote
# helper (`ext::`, `gcrypt::`), and an `@` after the colon is a mistyped credential.
_SCP_NO_USER = re.compile(r"^(?P<host>[^@:/\s]{2,}):(?P<path>[^\s:@][^\s@]*)$")
_PATH_PARAMS = re.compile(r"[?;]")


def _is_local_host(host: str) -> bool:
    """A loopback or unspecified host: no two machines mean the same server by it."""
    bare = host.strip("[]").rstrip(".")
    if not bare or bare == "localhost" or bare.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(bare)
    except ValueError:
        try:  # the short forms the C resolver takes: 127.1, 0x7f.0.0.1, 2130706433
            packed = socket.inet_aton(bare)
        except (OSError, ValueError):
            return False
        return packed[0] == 127 or not any(packed)
    mapped = getattr(address, "ipv4_mapped", None)
    address = mapped or address
    return address.is_loopback or address.is_unspecified


def project_identity(url: object) -> str | None:
    """Machine-independent identity of a network git remote, or ``None``.

    ``https``/``http``/``git`` URLs, and scp or ``ssh`` forms with user ``git``
    or no user, are ``canonicalize_remote_url``'s ``host/path`` with a
    non-default port kept as ``host:port/path`` and a trailing ``.git/``
    normalized, so ``git@host:foo/bar`` equals ``https://host/foo/bar``. An
    ``ssh`` or scp form with another user keeps only that user name,
    ``user@host/path`` with a leading ``/`` kept, so ``alice@host:repo``,
    ``bob@host:repo`` and ``alice@host:/repo`` all differ; a password, and a
    query or parameters on the last path segment, never survive, and ``?`` or
    ``;`` in an earlier segment means no identity. Local paths, ``file://``, a
    loopback or unspecified host, a remote helper (``gcrypt::``), a host-only
    form, a control character and a malformed URL or port have no identity.
    This name authorizes writes and deletions, so two
    repositories on one host's different ports never merge; ``retro-fleet``'s
    grouping keeps using the looser ``canonicalize_remote_url``.
    """
    if not isinstance(url, str):
        return None
    s = url.strip()
    if not s or any(ord(c) < 32 or ord(c) == 127 for c in s):
        return None
    user: str | None
    port: int | None = None
    scheme: str
    if "://" in s:
        try:
            parts = urlsplit(s)
            port = parts.port
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        if scheme not in _DEFAULT_PORTS:
            return None
        host = (parts.hostname or "").lower()
        user = parts.username if scheme == "ssh" else None
        path = parts.path or ""
        if scheme in ("ssh", "git"):  # literal path characters there
            path += f"?{parts.query}" if parts.query else ""
            path += f"#{parts.fragment}" if parts.fragment else ""
    else:
        m = _SCP_FORM.match(s) or _SCP_NO_USER.match(s)
        if m is None:
            return None
        scheme = "ssh"
        host = m.group("host").lower()
        user = m.groupdict().get("user")
        path = m.group("path")
    if not host or _is_local_host(host):
        return None
    # Only the last segment can carry a query or parameters. Anywhere else they
    # could hide a token or tell two repositories apart, so there is no identity.
    head, slash, tail = path.rstrip("/").rpartition("/")
    if _PATH_PARAMS.search(head):
        return None
    path = (head + slash + _PATH_PARAMS.split(tail, maxsplit=1)[0]).rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.rstrip("/")
    other_user = scheme == "ssh" and bool(user) and user != "git"
    if not path.strip("/"):
        return None
    if not other_user:
        path = path.lstrip("/")
    port_part = f":{port}" if port is not None and port != _DEFAULT_PORTS[scheme] else ""
    if other_user:
        return f"{user}@{host}{port_part}/{path}"
    return f"{host}{port_part}/{path}"


def git_wire(identity: str) -> str:
    """The wire folder name for an identity."""
    return GIT_PREFIX + quote(identity, safe="")


def wire_identity(wire_folder: str) -> str | None:
    """The identity named by a ``@git-`` wire folder, or ``None``."""
    if not wire_folder.startswith(GIT_PREFIX):
        return None
    return unquote(wire_folder[len(GIT_PREFIX) :]) or None


def display_name(wire: str) -> str:
    """A wire name for the terminal: percent-decoded first, sanitized last, so a
    ``%1B`` in a peer's name cannot reach the terminal as an escape."""
    text = unquote(wire[len(GIT_PREFIX) :]) if wire.startswith(GIT_PREFIX) else wire
    return safety.safe_terminal_str(text)


def unplaceable_reason(folder: str) -> str | None:
    """Why this machine cannot place a project folder name, reading the name only.

    ``@git-`` is ``unplaced`` (an identity with no checkout here), ``@old-`` is
    ``old_form`` (another home's names from a peer still on path names), and
    ``@home`` / ``@lit-`` are ``superseded`` (a stale or displaced copy of a
    project that is real on this machine). Folder names Claude creates never
    start with ``@``.
    """
    if not folder.startswith("@"):
        return None
    if folder.startswith(GIT_PREFIX):
        return "unplaced"
    if folder.startswith(OLD_PREFIX):
        return "old_form"
    if is_home_form(folder) or folder.startswith(LIT_PREFIX):
        return "superseded"
    return "unplaced"


def is_home_form(folder: str) -> bool:
    return folder == HOME_NAME or folder.startswith(HOME_NAME + "-")


def carries_a_deletion(folder: str) -> bool:
    """True for a wire name whose tombstones still mean something if it stays literal.

    ``@git-`` and ``@home`` forms map back to a local folder when the owner or
    home returns, so this machine's own deletion records under them are kept
    while they stay literal. ``@old-`` and ``@lit-`` names never map back.
    """
    return folder.startswith(GIT_PREFIX) or is_home_form(folder)


# ---------------------------------------------------------------------------
# Path encoding and the machine's home
# ---------------------------------------------------------------------------


def encode_path(path: object) -> str:
    """Claude's folder name for a checkout path: every non-alphanumeric to ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _fs_root() -> Path:
    """The filesystem root decoding starts from; a test seam."""
    return Path("/")


def _wire_home() -> Path | None:
    """This machine's resolved home, or ``None`` on any failure (codec off)."""
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(WIRE_HOME_TEST_ENV):
        return None
    try:
        return Path.home().resolve()
    except Exception:
        return None


def encoded_home() -> str | None:
    """The encoded name of this machine's home, or ``None`` when unresolvable."""
    home = _wire_home()
    if home is None:
        return None
    try:
        rel = home.relative_to(_fs_root())
    except ValueError:
        return None
    if not rel.parts:
        return None
    return encode_path("/" + str(rel))


def home_failure() -> str | None:
    """The exception class that kept the home from resolving, else ``None``."""
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(WIRE_HOME_TEST_ENV):
        return None  # the test guard keeps the codec off on purpose; not a failure
    try:
        Path.home().resolve()
    except Exception as exc:
        return type(exc).__name__
    return None if encoded_home() is not None else "ValueError"


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class Row:
    folder: str
    form: str  # "git" | "home" | "literal"
    identity: str | None
    why: str | None


@dataclass
class Clone:
    identity: str
    owner: str
    others: list[str]


@dataclass
class CwdInfo:
    identity: str | None
    owner: str | None
    why: str | None


@dataclass
class Index:
    """One frozen snapshot: decode, tombstones and encode all read this."""

    folders: tuple[str, ...]
    rows: tuple[Row, ...]
    owners: Mapping[str, str]  # identity -> owning folder
    folder_identity: Mapping[str, str]  # owning folder -> identity
    clones: tuple[Clone, ...]
    decoded: int
    timed_out: bool
    cwd: CwdInfo | None
    claude_dir: Path
    claude_real: str
    home_enc: str | None
    listed: bool = True  # False: `projects/` could not be listed, so absence proves nothing
    # First switch only: a removed folder -> the identity its deletion records publish
    # under, so a later pull in that checkout can honour them. It owns nothing here.
    record_identity: Mapping[str, str] = field(default_factory=dict)

    @property
    def owned(self) -> int:
        return sum(1 for r in self.rows if r.form == "git")

    def to_wire(self, folder: str) -> str:
        """The wire name a local project folder publishes under."""
        identity = self.folder_identity.get(folder)
        if identity is not None:
            return git_wire(identity)
        home = self.home_enc
        if home is not None:
            if folder == home:
                return HOME_NAME
            if folder.startswith(home + "-"):
                return HOME_NAME + folder[len(home) :]
        return folder


# The sidecar's ``wire_folders`` (wire name -> local folder), or a callable that
# reads them when the index is built.
Retained = Mapping[str, Any] | Callable[[], Mapping[str, Any] | None] | None


# The folders a sidecar from before the first switch names, or a callable reading them.
PreSwitch = set[str] | Callable[[], set[str] | None] | None


@dataclass
class _Context:
    claude_dir: Path | None = None
    retained: Retained = None
    pre_switch: PreSwitch = None
    cwd_candidate: bool = False
    index: Index | None = None
    home_checked: bool = False
    home_enc: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


_CTX = _Context()


def _retained_by_folder(retained: Retained) -> dict[str, str]:
    """``folder -> identity`` from the sidecar's ``wire_folders``; never raises."""
    try:
        recorded = retained() if callable(retained) else retained
        by_folder: dict[str, str] = {}
        for wire, folder in (recorded or {}).items():
            identity = wire_identity(wire) if isinstance(wire, str) else None
            if identity is not None and isinstance(folder, str) and folder:
                by_folder[folder] = identity
        return by_folder
    except Exception:
        return {}


def _pre_switch_folders(pre_switch: PreSwitch) -> set[str]:
    """Local folder names from a sidecar written before the first switch; never raises."""
    try:
        recorded = pre_switch() if callable(pre_switch) else pre_switch
        return {f for f in (recorded or ()) if isinstance(f, str) and f.startswith("-")}
    except Exception:
        return set()


def configure(
    claude_dir: Path | str | None, *, retained: Retained = None, pre_switch: PreSwitch = None
) -> None:
    """Set the process-wide context and drop any built index.

    ``retained`` is the sidecar's ``wire_folders`` (wire name -> local folder),
    or a callable returning them, read only when the index is built: a folder
    whose resolution later times out or cannot be read keeps the identity
    recorded for it there, and so does a folder deleted while its checkout lives.
    ``pre_switch`` names the folders of a sidecar from before the first switch,
    which records no identities. A removed one is resolved too, only to spell its
    deletion records: see ``Index.record_identity``.
    """
    with _CTX.lock:
        _CTX.claude_dir = Path(claude_dir) if claude_dir is not None else None
        _CTX.retained = retained
        _CTX.pre_switch = pre_switch
        _CTX.cwd_candidate = False
        _CTX.index = None
        _CTX.home_checked = False
        _CTX.home_enc = None


def reset() -> None:
    """Clear the context (tests)."""
    configure(None)


def allow_cwd_candidate() -> None:
    """Let the current directory's main checkout own an identity nobody else owns.

    Called at ``_pull_core`` entry, before the first decode, so only the pull
    paths (``mm pull``, its dry run, ``mm autopull`` and the join's pull) use it.
    """
    with _CTX.lock:
        if not _CTX.cwd_candidate:
            _CTX.cwd_candidate = True
            _CTX.index = None


def _home_enc() -> str | None:
    """This process's encoded home, resolved once per ``configure``."""
    if not _CTX.home_checked:
        _CTX.home_enc = encoded_home()
        _CTX.home_checked = True
    return _CTX.home_enc


def enabled() -> bool:
    """True when the codec runs: a configured Claude folder and a resolved home."""
    return _CTX.claude_dir is not None and _home_enc() is not None


def configured() -> bool:
    return _CTX.claude_dir is not None


def claude_real() -> str | None:
    """The resolved Claude folder, in the form a walker records as ``base_path``."""
    return os.path.realpath(_CTX.claude_dir) if _CTX.claude_dir is not None else None


def projects_listed() -> bool:
    """False when the Claude ``projects/`` folder cannot be listed right now."""
    if _CTX.index is not None:
        return _CTX.index.listed
    if _CTX.claude_dir is None:
        return True
    return _list_project_folders(_CTX.claude_dir / "projects") is not None


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class _Unknown(Exception):
    """A read failed for a reason other than the path being gone."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


_GONE = (FileNotFoundError, NotADirectoryError)


class _Memo:
    """Per-run directory-listing memo shared by every folder's descent."""

    def __init__(self) -> None:
        self._listings: dict[Path, list[tuple[str, str]]] = {}

    def subdirs(self, directory: Path) -> list[tuple[str, str]]:
        cached = self._listings.get(directory)
        if cached is not None:
            return cached
        out: list[tuple[str, str]] = []
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            out.append((entry.name, encode_path(entry.name)))
                    except OSError:
                        continue
        except _GONE:
            pass
        except OSError as exc:
            raise _Unknown(WHY_UNREADABLE) from exc
        self._listings[directory] = out
        return out


@dataclass
class _Resolution:
    kind: str  # "identity" | "nominate" | "none" | "unknown"
    identity: str | None = None
    why: str | None = None
    nominee: Path | None = None
    has_memory: bool = False
    decoded: bool = False


def _decode(folder: str, root: Path, memo: _Memo) -> list[Path]:
    """Directories whose encoded path is ``folder``, found by descent from ``root``.

    Never follows a symlink. Stops at a second match: more than one is ambiguous.
    """
    out: list[Path] = []
    if not folder.startswith("-"):
        return out

    def walk(directory: Path, remaining: str) -> None:
        for name, enc in memo.subdirs(directory):
            candidate = "-" + enc
            if remaining == candidate:
                out.append(directory / name)
            elif remaining.startswith(candidate + "-"):
                walk(directory / name, remaining[len(candidate) :])
            if len(out) > 1:
                return

    walk(root, folder)
    return out


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except _GONE:
        return None
    except OSError as exc:
        raise _Unknown(WHY_UNREADABLE) from exc


_NEEDS_GIT = re.compile(r'["\\\s#;]')


def _read_origin(checkout: Path) -> str:
    """The ``origin`` URL of a main checkout, ``""`` when it has none.

    Parses ``.git/config`` with the standard library (equal to ``git config
    --get`` on every repository measured); a quoted or escaped value, an inline
    comment or continued line, a section spelling the parser does not see, an
    ``include`` or a parse failure asks git itself, with its 2 s timeout.
    """
    config_path = checkout / ".git" / "config"
    try:
        if not stat.S_ISREG(os.stat(config_path).st_mode):
            raise _Unknown(WHY_UNREADABLE)  # a FIFO would block the read forever
        text = config_path.read_text(encoding="utf-8")
    except _GONE:
        return ""
    except UnicodeDecodeError:
        return _git_origin(checkout)
    except OSError as exc:
        raise _Unknown(WHY_UNREADABLE) from exc
    parser = configparser.RawConfigParser(strict=False, allow_no_value=True, delimiters=("=",))
    try:
        parser.read_string(text)
        has_include = any(s.lower().startswith("include") for s in parser.sections())
        section = 'remote "origin"'
        found = parser.has_section(section) and parser.has_option(section, "url")
        value = (parser.get(section, "url") or "").strip() if found else ""
    except Exception:
        return _git_origin(checkout)
    # Quotes, escapes, an inline comment or a continued line: git decides. So does
    # a spelling this parser does not see: `[remote.origin]`, another case.
    if has_include or _NEEDS_GIT.search(value) or (not found and "origin" in text.lower()):
        return _git_origin(checkout)
    return value


def _git_origin(checkout: Path) -> str:
    try:
        result = subprocess.run(
            # This file only, by absolute path: `-C` or a relative path can reach a
            # parent repository when this `.git` is not one git accepts.
            ["git", "config", "--file", str(checkout / ".git" / "config"), "--includes"]
            + ["--get", "remote.origin.url"],
            cwd=checkout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=gitenv.scrubbed_git_env(),
            timeout=2,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise _Unknown(WHY_UNREADABLE) from exc
    if result.returncode == 1:
        return ""
    if result.returncode != 0:
        raise _Unknown(WHY_UNREADABLE)
    return result.stdout.strip()


def _linked_main_checkout(path: Path) -> Path | None:
    """The main checkout a linked worktree at ``path`` belongs to, else ``None``.

    ``.git`` is a file naming a git dir whose ``commondir`` is a ``.git``
    directory: that directory's parent is the main checkout. A submodule or a
    bare layout has no ``commondir`` named ``.git`` and nominates nothing.
    Reads that fail for a reason other than absence raise ``_Unknown``.
    """
    dot_git = path / ".git"
    try:
        raw = dot_git.read_bytes()
    except _GONE:
        return None
    except OSError as exc:
        raise _Unknown(WHY_UNREADABLE) from exc
    try:
        text = raw.split(b"\n", 1)[0].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.startswith("gitdir: "):
        return None
    target = text[len("gitdir: ") :].strip()
    if not target:
        return None
    gitdir = Path(target)
    if not gitdir.is_absolute():
        gitdir = dot_git.parent / gitdir
    try:
        commondir_raw = (gitdir / "commondir").read_bytes()
    except _GONE:
        return None
    except OSError as exc:
        raise _Unknown(WHY_UNREADABLE) from exc
    try:
        rel = commondir_raw.split(b"\n", 1)[0].decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not rel:
        return None
    common = Path(rel)
    if not common.is_absolute():
        common = gitdir / common
    try:
        common = common.resolve()
    except (OSError, RuntimeError) as exc:
        raise _Unknown(WHY_UNREADABLE) from exc
    if common.name != ".git":
        return None
    head = _lstat(common / "HEAD")
    if head is None or not stat.S_ISREG(head.st_mode):
        return None
    return common.parent


def _checkout_identity(checkout: Path) -> tuple[str | None, str | None]:
    """``(identity, why)`` for a main checkout; exactly one is ``None``."""
    url = _read_origin(checkout)
    if not url:
        return None, WHY_NO_ORIGIN
    identity = project_identity(url)
    if identity is None:
        return None, WHY_LOCAL_REMOTE
    return identity, None


def _probe_checkout(path: Path) -> _Resolution:
    """What the directory ``path`` is: a main checkout, a linked worktree, or neither."""
    dot_git = path / ".git"
    st = _lstat(dot_git)
    if st is None:
        return _Resolution("none", why=WHY_NO_CHECKOUT)
    if stat.S_ISDIR(st.st_mode):
        head = _lstat(dot_git / "HEAD")
        if head is None or not stat.S_ISREG(head.st_mode):
            return _Resolution("none", why=WHY_NO_CHECKOUT)
        identity, why = _checkout_identity(path)
        if identity is None:
            return _Resolution("none", why=why)
        return _Resolution("identity", identity=identity)
    if stat.S_ISREG(st.st_mode):
        main = _linked_main_checkout(path)
        if main is None:
            return _Resolution("none", why=WHY_SUBMODULE)
        identity, _why = _checkout_identity(main)
        return _Resolution("nominate", identity=identity, why=WHY_WORKTREE, nominee=main)
    return _Resolution("none", why=WHY_NO_CHECKOUT)


def _resolve_folder(folder: str, projects: Path, root: Path, memo: _Memo) -> _Resolution:
    has_memory = False
    try:
        mem = os.lstat(projects / folder / "memory")
        has_memory = stat.S_ISDIR(mem.st_mode)
    except OSError:
        pass
    try:
        found = _decode(folder, root, memo)
        if not found:
            res = _Resolution("none", why=WHY_NO_CHECKOUT)
        elif len(found) > 1:
            res = _Resolution("none", why=WHY_AMBIGUOUS, decoded=False)
        else:
            res = _probe_checkout(found[0])
            res.decoded = True
    except _Unknown as exc:
        res = _Resolution("unknown", why=exc.why)
    except Exception:  # one odd folder (a NUL in a .git file) never stops the others
        res = _Resolution("unknown", why=WHY_UNREADABLE)
    res.has_memory = has_memory
    return res


class _Worker:
    """Resolves folders on a daemon thread; appends only to its own list."""

    def __init__(self, folders: list[str], projects: Path, root: Path) -> None:
        self.folders = folders
        self.projects = projects
        self.root = root
        self.memo = _Memo()
        self.results: list[tuple[str, _Resolution]] = []
        self.cancelled = False

    def run(self) -> None:
        for folder in self.folders:
            if self.cancelled:
                return
            self.results.append(
                (folder, _resolve_folder(folder, self.projects, self.root, self.memo))
            )


def _list_project_folders(projects: Path) -> list[str] | None:
    """The folders under ``projects/``, or ``None`` when it cannot be listed."""
    names: list[str] = []
    try:
        with os.scandir(projects) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return None
    return sorted(names)


def _cwd_nomination(root: Path) -> tuple[CwdInfo, str | None, str | None]:
    """The current directory's main checkout: ``(info, identity, folder)``."""
    try:
        here = Path.cwd()
    except OSError:
        return CwdInfo(None, None, WHY_UNREADABLE), None, None
    try:
        for candidate in (here, *here.parents):
            dot_git = candidate / ".git"
            st = _lstat(dot_git)
            if st is None:
                continue
            if stat.S_ISDIR(st.st_mode):
                head = _lstat(dot_git / "HEAD")
                if head is None or not stat.S_ISREG(head.st_mode):
                    return CwdInfo(None, None, WHY_NO_CHECKOUT), None, None
                main: Path | None = candidate
            elif stat.S_ISREG(st.st_mode):
                main = _linked_main_checkout(candidate)
                if main is None:
                    return CwdInfo(None, None, WHY_SUBMODULE), None, None
            else:
                return CwdInfo(None, None, WHY_NO_CHECKOUT), None, None
            assert main is not None
            identity, why = _checkout_identity(main)
            if identity is None:
                return CwdInfo(None, None, why), None, None
            try:
                rel = main.relative_to(root)
            except ValueError:
                return CwdInfo(identity, None, WHY_NO_CHECKOUT), None, None
            return CwdInfo(identity, None, None), identity, encode_path("/" + str(rel))
    except _Unknown as exc:
        return CwdInfo(None, None, exc.why), None, None
    except Exception:
        return CwdInfo(None, None, WHY_UNREADABLE), None, None
    return CwdInfo(None, None, WHY_NO_CHECKOUT), None, None


def _build_index(
    claude_dir: Path,
    retained: Mapping[str, str],
    cwd_candidate: bool,
    home: str | None,
    pre_switch: set[str] | frozenset[str] = frozenset(),
) -> Index:
    root = _fs_root()
    projects = claude_dir / "projects"
    listing = _list_project_folders(projects)
    listed = listing is not None
    folders = listing or []
    # A folder deleted from `projects/` is still resolved when it published an
    # identity: its checkout may be alive, so the deletion is a real one. One a
    # pre-switch sidecar names is resolved only to spell its deletion records. With
    # `projects/` unlisted the owner still stands, so a pull restores the project;
    # `manifest` is what keeps that outage from minting.
    deleted = sorted(set(retained) - set(folders))
    removed_before_switch = sorted(set(pre_switch) - set(folders) - set(retained))
    worker = _Worker([*folders, *deleted, *removed_before_switch], projects, root)
    thread = threading.Thread(target=worker.run, name="mm-project-index", daemon=True)
    deadline = time.monotonic() + INDEX_DEADLINE_SECONDS
    thread.start()
    cwd_box: list[tuple[CwdInfo, str | None, str | None]] = []
    cwd_thread: threading.Thread | None = None
    if cwd_candidate:
        cwd_thread = threading.Thread(
            target=lambda: cwd_box.append(_cwd_nomination(root)), name="mm-project-cwd", daemon=True
        )
        cwd_thread.start()
    thread.join(max(0.0, deadline - time.monotonic()))
    if cwd_thread is not None:
        cwd_thread.join(max(0.0, deadline - time.monotonic()))
    timed_out = thread.is_alive() or (cwd_thread is not None and cwd_thread.is_alive())
    worker.cancelled = True
    # Freeze one immutable snapshot: a worker finishing late appends to a list
    # this function never reads again.
    done = dict(list(worker.results))
    resolutions: dict[str, _Resolution] = {
        f: done.get(f) or _Resolution("unknown", why=WHY_TIMED_OUT) for f in folders
    }

    # Candidate tiers per identity: resolved folders, then retained identities
    # for folders that could not be read, then a folder deleted while its
    # checkout lives, then nominations.
    resolved: dict[str, list[str]] = {}
    held: dict[str, list[str]] = {}
    kept: dict[str, list[str]] = {}
    nominated: dict[str, set[str]] = {}
    for folder, res in resolutions.items():
        if res.kind == "identity" and res.identity:
            resolved.setdefault(res.identity, []).append(folder)
        elif res.kind == "unknown" and folder in retained:
            held.setdefault(retained[folder], []).append(folder)
        elif res.kind == "nominate" and res.identity and res.nominee is not None:
            try:
                rel = res.nominee.relative_to(root)
            except ValueError:
                continue
            nominated.setdefault(res.identity, set()).add(encode_path("/" + str(rel)))
    for folder in deleted:
        res = done.get(folder)
        # Only a checkout read now proves a deletion. Unreadable or out of time, a
        # moved checkout may be behind it, and a false tombstone blocks the fleet.
        if res is not None and res.kind == "identity" and res.identity == retained[folder]:
            kept.setdefault(res.identity, []).append(folder)

    cwd_info: CwdInfo | None = None
    if cwd_candidate:
        if cwd_box:
            cwd_info, cwd_identity, cwd_folder = cwd_box[0]
            if cwd_identity is not None and cwd_folder is not None:
                nominated.setdefault(cwd_identity, set()).add(cwd_folder)
        else:
            cwd_info = CwdInfo(None, None, WHY_TIMED_OUT)

    def rank(folder: str) -> tuple[bool, str]:
        return (not resolutions[folder].has_memory, folder)

    owners: dict[str, str] = {}
    clones: list[Clone] = []
    taken: set[str] = set()

    def own(identity: str, cands: list[str]) -> None:
        owners[identity] = cands[0]
        taken.add(cands[0])
        if len(cands) > 1:
            clones.append(Clone(identity, cands[0], cands[1:]))

    for identity in sorted({*resolved, *held, *kept}):
        if resolved.get(identity):
            own(identity, sorted(resolved[identity], key=rank))
        elif held.get(identity):
            own(identity, sorted(held[identity], key=rank))
        else:
            own(identity, sorted(kept[identity]))
    # A nomination is an encoded path, which two checkouts can share (`a-b`,
    # `a.b`): a folder has one owner, so the first identity keeps it.
    for identity in sorted(set(nominated) - set(owners)):
        cands = sorted(nominated[identity] - taken)
        if cands:
            own(identity, cands)
    folder_identity = {folder: identity for identity, folder in owners.items()}
    record_identity: dict[str, str] = {}
    for folder in removed_before_switch:
        res = done.get(folder)
        if res is not None and res.kind == "identity" and res.identity:
            if res.identity not in owners:
                record_identity[folder] = res.identity
    if cwd_info is not None and cwd_info.identity is not None:
        cwd_info.owner = owners.get(cwd_info.identity)

    rows: list[Row] = []
    for folder in folders:
        res = resolutions[folder]
        if folder in folder_identity:
            rows.append(Row(folder, "git", folder_identity[folder], None))
            continue
        if res.kind == "identity":
            why: str | None = WHY_SECOND_CLONE
        elif res.kind == "unknown":
            why = res.why or WHY_UNREADABLE
        else:
            why = res.why
        form = (
            "home"
            if home is not None and (folder == home or folder.startswith(home + "-"))
            else "literal"
        )
        rows.append(Row(folder, form, res.identity if res.kind == "identity" else None, why))
    return Index(
        folders=tuple(folders),
        rows=tuple(rows),
        owners=owners,
        folder_identity=folder_identity,
        clones=tuple(clones),
        decoded=sum(1 for r in resolutions.values() if r.decoded),
        timed_out=timed_out,
        cwd=cwd_info,
        claude_dir=claude_dir,
        claude_real=os.path.realpath(claude_dir),
        home_enc=home,
        listed=listed,
        record_identity=record_identity,
    )


def index() -> Index | None:
    """The process's project index, built on first use; ``None`` when the codec is off."""
    with _CTX.lock:
        if _CTX.index is None:
            if _CTX.claude_dir is None or _home_enc() is None:
                return None
            _CTX.index = _build_index(
                _CTX.claude_dir,
                _retained_by_folder(_CTX.retained),
                _CTX.cwd_candidate,
                _CTX.home_enc,
                _pre_switch_folders(_CTX.pre_switch),
            )
        return _CTX.index


def encoding_suspect() -> bool:
    """True when Claude-created folders exist and none decodes to a directory.

    A folder is Claude-created when listing it shows a ``*.jsonl``; nothing is
    read. This is the sign that Claude's folder naming changed on this machine.
    An index that could not finish proves nothing about the naming.
    """
    idx = index()
    if idx is None or idx.decoded > 0 or not idx.folders or idx.timed_out:
        return False
    if any(r.why in (WHY_TIMED_OUT, WHY_UNREADABLE) for r in idx.rows):
        return False
    projects = idx.claude_dir / "projects"
    for folder in idx.folders:
        try:
            with os.scandir(projects / folder) as it:
                if any(entry.name.endswith(".jsonl") for entry in it):
                    return True
        except OSError:
            continue
    return False


def diag_state() -> dict[str, Any]:
    """The index as ``mm diag --json`` reports it (no peers, no passphrase)."""
    idx = index()
    if idx is None:
        return {
            "state": None,
            "reason": "home_unresolved" if configured() else None,
            "folders": 0,
            "decoded": 0,
            "owned": 0,
            "timed_out": False,
            "clones": [],
            "rows": [],
        }
    reason = "encoding_mismatch" if encoding_suspect() else None
    return {
        "state": None,
        "reason": reason,
        "folders": len(idx.folders),
        "decoded": idx.decoded,
        "owned": idx.owned,
        "timed_out": idx.timed_out,
        "clones": [
            {"identity": safety.safe_terminal_str(c.identity), "owner": c.owner, "others": c.others}
            for c in idx.clones
        ],
        "rows": [
            {
                "folder": r.folder,
                "form": r.form,
                "identity": (safety.safe_terminal_str(r.identity) if r.identity else None),
                "why": r.why,
            }
            for r in idx.rows
        ],
    }
