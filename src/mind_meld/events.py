"""Per-push event capture for fleet-aware retro (Group 7 — Track 7A).

Every push appends 1 + N + M events to a per-device daily JSONL file under
the mm-events synced source. Three event types per push:

  - mm-push           one per push, the cursor anchor for the next push
  - git-snapshot      per-repo commit metadata (deduped fleet-wide by canonical
                      remote URL + sha at retro render time)
  - sessions-snapshot per-project Claude Code session metadata

The events log itself is the cursor (no separate cursor file): the most
recent `mm-push` event's `ts` answers "since when do I scan?" on the next
push. Pattern matches `pullhistory.py` (the log file is the state of truth).

Same-push upload semantics. Track 7B's wiring runs the events tail at the
HEAD of `_push_core` (BEFORE `build_manifest_v2`), so the just-written
events file IS picked up by the manifest build and uploaded as part of the
SAME push. (Pre-Track-7B prototypes ran a true tail-position write that
required next-push lag; the production wiring eliminated that lag — see
CLAUDE.md "Events tail in _push_core" for the locked invariants.)

Trust boundary. Track 7B's `_push_core` wiring MUST run the events tail on
EVERY push attempt, including no-content-diff early returns. Without it,
machines that push regularly but rarely change content silently never
advance their cursor and never appear in fleet retros.

Init-time backfill. cli.py's `_run_events_backfill` runs at the end of
`mm init` and writes a 30-day git-snapshot + full sessions-snapshot, but
NO mm-push event. Lets retro-fleet work immediately after init without
waiting for the first push. The aggregator dedups commits via
(canonical_remote_url, sha), so the first real push re-walking the same
30-day window is harmless.

Schema (v=1, total=False — forward-compat readers tolerate unknown fields):
see TypedDict definitions below.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import (
    CancelledError,
    Future,
    ThreadPoolExecutor,
    as_completed,
)
from concurrent.futures import (
    TimeoutError as FuturesTimeoutError,
)
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

from mind_meld import fsutil
from mind_meld.config import MM_INTERNAL_SOURCE_NAMES

# ---------------------------------------------------------------------------
# Module-level named constants (C3). Track 7B imports the budget pair to
# avoid redefining the numbers on the wiring side.
# ---------------------------------------------------------------------------

INITIAL_CURSOR_LOOKBACK_DAYS = 30
WALK_TIME_BUDGET_INTERACTIVE_MS = 500
WALK_TIME_BUDGET_AUTOPUSH_MS = 250
MAX_GIT_WORKERS = 8
PER_REPO_TIMEOUT_FLOOR_MS = 200
PER_REPO_TIMEOUT_CAP_MS = 2000

EVENTS_SCHEMA_VERSION = 2
"""Bumped from 1 → 2 for Group 8 (v0.11.0).

v=1 sessions-snapshot rows shipped by Track 7A had DELTA semantics: only
sessions whose mtime advanced since the cursor were counted. Naive sum of v=1
snapshots double-counted a single chat that was touched across pushes; latest-
only-wins undercounted by losing prior windows. Codex outside-voice review
caught the trap during /plan-eng-review for Group 8.

v=2 sessions-snapshot is FULL INVENTORY: every jsonl in the projects tree is
counted regardless of mtime. Aggregator picks the LATEST v=2 snapshot per
(device, claude_dir) — gives an accurate point-in-time sessions count for the
rendering machine's view of the fleet.

mm-push and git-snapshot rows keep delta semantics (commits since last push,
dedup-by-sha aggregator side). Only sessions-snapshot changed semantics.

Mixed-fleet transition: peers on pre-v0.11.0 still emit v=1 sessions rows.
The retro-fleet aggregator treats v=1 sessions as below-threshold and surfaces
'sessions count low for peer X — pre-v0.11.0' as part of the fleet-incomplete
breadcrumb. Numbers are honestly low, never overcounted. Once the fleet rolls
to v0.11.0, every peer emits v=2 and the count is exact."""

_GIT_LOG_FORMAT = "%x1e%H%x09%cI%x09%ae%x09%s"
"""Record-separator (\\x1e) prefix + tab-delimited fields:
SHA, commit-date (ISO 8601), author email, subject. Commit date matches
`git log --since` filter window (CT-13)."""


# ---------------------------------------------------------------------------
# v=1 schema (TypedDict). Functional form for GitCommit handles the `del`
# Python reserved word so the JSON field name stays "del" (CT-8).
# ---------------------------------------------------------------------------

GitCommit = TypedDict(
    "GitCommit",
    {
        "sha": str,
        "date": str,
        "author_email": str,
        "subject": str,
        "files": int,
        "add": int,
        "del": int,
    },
    total=False,
)


class GitSnapshotProject(TypedDict, total=False):
    remote: str
    local_path: str
    commits: list[GitCommit]


class GitSnapshotSkip(TypedDict, total=False):
    path: str
    reason: str


class GitSnapshot(TypedDict, total=False):
    v: int
    type: str
    ts: str
    device: str
    projects: list[GitSnapshotProject]
    skipped: list[GitSnapshotSkip]


class SessionMetadata(TypedDict, total=False):
    claude_dir: str
    source_root: str
    sessions: int
    total_kb: int
    last_session_at: str
    ephemeral: bool


class SessionsSnapshot(TypedDict, total=False):
    v: int
    type: str
    ts: str
    device: str
    projects: list[SessionMetadata]


class MmPushEvent(TypedDict, total=False):
    v: int
    type: str
    ts: str
    device: str
    mm_version: str
    sources: list[str]
    discovery_errors: list[str]


# ---------------------------------------------------------------------------
# canonicalize_remote_url — pure function, table-tested.
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


# ---------------------------------------------------------------------------
# discover_git_roots — multi-prober registry (A1).
# ---------------------------------------------------------------------------


def discover_git_roots(config: dict) -> tuple[list[Path], list[str]]:
    """Discover git repo roots via gstack + claude + manual probers.

    Returns ``(roots, errors)``. ``roots`` are deduped via ``Path.resolve()``
    (handles APFS case-mismatched paths). ``errors`` is a forensic trail —
    distinguishing "no repos configured" (clean machine) from "discovery
    broke" (CT-11). Adopters wire the ``errors`` list into the mm-push
    event's ``discovery_errors`` field so the fleet retro shows the
    breakage breadcrumb.

    Probers (each may individually fail; we always run all of them):
      1. gstack:  ``~/.gstack/projects/*/repo-mode.json`` when the gstack
                  source is enabled in the resolved sources.
      2. claude:  read the ``cwd`` field from the most recent jsonl in
                  ``~/.claude/projects/<encoded>/`` when the claude source
                  is enabled. Authoritative — avoids the lossy `-`-encoding
                  reverse engineering.
      3. manual:  ``config["retro"]["repo_roots"]: list[str]`` — escape
                  hatch + override.

    Filter: keep only paths where ``git rev-parse --show-toplevel`` succeeds
    (CT-1). Worktrees have ``.git`` as a FILE, not a directory — Conductor
    workspaces are worktrees and must not be silently excluded.
    """
    errors: list[str] = []
    candidates: list[Path] = []

    enabled_sources = _enabled_source_names(config)

    if "gstack" in enabled_sources:
        try:
            candidates.extend(_probe_gstack())
        except Exception as e:
            errors.append(f"gstack prober: {type(e).__name__}: {e}")

    if "claude" in enabled_sources:
        try:
            candidates.extend(_probe_claude())
        except Exception as e:
            errors.append(f"claude prober: {type(e).__name__}: {e}")

    try:
        manual = (config.get("retro", {}) or {}).get("repo_roots") or []
        candidates.extend(Path(p).expanduser() for p in manual if isinstance(p, str))
    except Exception as e:
        errors.append(f"manual prober: {type(e).__name__}: {e}")

    # Dedup via Path.resolve(). Paths that don't exist still resolve (they
    # just point at a non-existent target); the git-toplevel filter below
    # is what drops them.
    seen: set[Path] = set()
    deduped: list[Path] = []
    for c in candidates:
        try:
            r = c.resolve()
        except OSError:
            continue
        if r in seen:
            continue
        seen.add(r)
        deduped.append(r)

    # Filter: keep paths that are inside a git toplevel (per CT-1).
    roots: list[Path] = []
    for p in deduped:
        if _is_git_toplevel(p):
            roots.append(p)

    return roots, errors


def _enabled_source_names(config: dict) -> set[str]:
    """Resolve enabled source names without importing config.get_sources
    (which has its own bootstrap + filesystem probing semantics). We just
    need to know which probers to run — a strict mismatch with config
    semantics is forensic, not data integrity.
    """
    sync_cfg = (config or {}).get("sync", {}) or {}
    explicit = sync_cfg.get("sources")
    if isinstance(explicit, list):
        names = {s.get("name") for s in explicit if isinstance(s, dict)}
    else:
        # Default sources include claude, gstack (if ~/.gstack exists), mm-events.
        names = {"claude"}
        if (Path.home() / ".gstack").exists():
            names.add("gstack")
    disabled = set(sync_cfg.get("disabled_sources") or [])
    return {n for n in names if isinstance(n, str) and n not in disabled}


def _probe_gstack() -> list[Path]:
    """Read ~/.gstack/projects/*/repo-mode.json and extract repo_root paths.

    Best-effort per file: malformed JSON or missing field skips that
    project, keeps walking. Empty list on no gstack dir.
    """
    base = Path.home() / ".gstack" / "projects"
    if not base.exists():
        return []
    out: list[Path] = []
    try:
        slugs = list(base.iterdir())
    except OSError:
        return []
    for slug_dir in slugs:
        rmf = slug_dir / "repo-mode.json"
        if not rmf.is_file():
            continue
        try:
            data = json.loads(rmf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # gstack writes repo_root as a direct field; tolerate alternates.
        for key in ("repo_root", "repo_path", "root"):
            v = data.get(key) if isinstance(data, dict) else None
            if isinstance(v, str) and v:
                out.append(Path(v).expanduser())
                break
    return out


def _probe_claude() -> list[Path]:
    """Walk ~/.claude/projects/<encoded>/ and read each project's `cwd`
    field from its most recent .jsonl session file.

    Reading `cwd` from session content avoids the lossy `-`-encoding reverse
    engineering (which can't disambiguate `-` vs `/` on paths containing
    hyphens). Authoritative source.
    """
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return []
    out: list[Path] = []
    try:
        project_dirs = list(base.iterdir())
    except OSError:
        return []
    for proj_dir in project_dirs:
        if not proj_dir.is_dir():
            continue
        cwd = _read_cwd_from_latest_jsonl(proj_dir)
        if cwd:
            out.append(Path(cwd).expanduser())
    return out


def _read_cwd_from_latest_jsonl(proj_dir: Path) -> str | None:
    """Find a `cwd` field in the most recent .jsonl session file. Falls
    back to older files if the newest is empty/corrupt."""
    try:
        jsonls = sorted(
            (p for p in proj_dir.iterdir() if p.suffix == ".jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for jl in jsonls:
        try:
            with open(jl, encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    cwd = obj.get("cwd") if isinstance(obj, dict) else None
                    if isinstance(cwd, str) and cwd:
                        return cwd
        except OSError:
            continue
    return None


def _is_git_toplevel(path: Path) -> bool:
    """Return True iff `git -C path rev-parse --show-toplevel` returns
    `path` itself (resolved). Worktrees succeed; non-git dirs fail. This
    handles `.git`-as-FILE (worktrees) and `.git`-as-DIRECTORY uniformly
    (CT-1 — a `.git/`-dir-existence check would silently exclude Conductor
    workspaces, which are themselves worktrees)."""
    if not path.is_dir():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        toplevel = Path(result.stdout.strip()).resolve()
        return toplevel == path.resolve()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# walk_git_projects — bounded fan-out over `git log --since`.
# ---------------------------------------------------------------------------


def walk_git_projects(
    roots: list[Path],
    since: datetime,
    total_budget_ms: int,
) -> list[GitSnapshot]:
    """Capture commits since `since` across `roots`. Returns one
    GitSnapshot per push (a SINGLE row aggregating all projects), so the
    pull-merge set-union semantics in `merge_jsonl` continue to work
    cleanly when two devices push at the same wall-clock instant.

    Wall-time enforcement (CT-6): submit all repos to a ThreadPoolExecutor,
    then pump `as_completed(timeout=remaining)`. On TimeoutError, cancel
    pending futures (running ones can't be cancelled but are orphaned;
    process exit reaps them when mm push exits).

    Per-repo timeout (A2):
        max(PER_REPO_TIMEOUT_FLOOR_MS,
            (total_budget_ms * MAX_GIT_WORKERS) // repos_remaining)
        capped at PER_REPO_TIMEOUT_CAP_MS

    Per-repo failures fold into the row's `skipped` field (A5). Whole-walk
    failure surfaces as `mm: notice:` and we return an empty/partial row.
    """
    ts_now = datetime.now(timezone.utc).isoformat()
    snapshot: GitSnapshot = {
        "v": EVENTS_SCHEMA_VERSION,
        "type": "git-snapshot",
        "ts": ts_now,
        "device": "",  # caller fills in via write_push_event composition
    }
    projects: list[GitSnapshotProject] = []
    skipped: list[GitSnapshotSkip] = []

    if not roots:
        snapshot["projects"] = projects
        snapshot["skipped"] = skipped
        return [snapshot]

    n_repos = len(roots)
    per_repo_timeout_ms = min(
        PER_REPO_TIMEOUT_CAP_MS,
        max(
            PER_REPO_TIMEOUT_FLOOR_MS,
            (total_budget_ms * MAX_GIT_WORKERS) // n_repos,
        ),
    )

    deadline = time.monotonic() + (total_budget_ms / 1000.0)
    since_iso = since.isoformat()

    executor = ThreadPoolExecutor(max_workers=MAX_GIT_WORKERS)
    try:
        futures: dict[Future, Path] = {
            executor.submit(_walk_one_repo, root, since_iso, per_repo_timeout_ms): root
            for root in roots
        }
        try:
            for fut in as_completed(futures, timeout=max(0.0, deadline - time.monotonic())):
                root = futures[fut]
                try:
                    proj, err = fut.result(timeout=0)
                except (CancelledError, FuturesTimeoutError, Exception) as e:
                    skipped.append({"path": str(root), "reason": f"{type(e).__name__}: {e}"})
                    continue
                if err:
                    skipped.append({"path": str(root), "reason": err})
                if proj is not None:
                    projects.append(proj)
        except FuturesTimeoutError:
            # Budget exhausted at the pump. Cancel pending; collect what's done.
            for fut, root in futures.items():
                if fut.done():
                    try:
                        proj, err = fut.result(timeout=0)
                    except Exception as e:
                        skipped.append({"path": str(root), "reason": f"{type(e).__name__}: {e}"})
                        continue
                    if err:
                        skipped.append({"path": str(root), "reason": err})
                    if proj is not None:
                        projects.append(proj)
                else:
                    fut.cancel()
                    skipped.append({"path": str(root), "reason": "budget_abort"})
    except Exception as e:
        sys.stderr.write(
            f"mm: notice: walk_git_projects whole-walk failure ({type(e).__name__}: {e})\n"
        )
    finally:
        # Don't wait for orphaned subprocesses: they'll be killed on process
        # exit. shutdown(wait=False) lets us return promptly even if a
        # future is still running.
        executor.shutdown(wait=False, cancel_futures=True)

    snapshot["projects"] = projects
    snapshot["skipped"] = skipped
    return [snapshot]


def _walk_one_repo(
    root: Path,
    since_iso: str,
    timeout_ms: int,
) -> tuple[GitSnapshotProject | None, str | None]:
    """Run `git log --since --numstat -M -C` and parse output. Returns
    (project, error). On any failure, project is None and error is set."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                f"--since={since_iso}",
                "--numstat",
                "-M",
                "-C",
                f"--format={_GIT_LOG_FORMAT}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000.0,
        )
    except subprocess.TimeoutExpired:
        return None, "TimeoutExpired"
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"
    if result.returncode != 0:
        return None, f"git log rc={result.returncode}: {result.stderr.strip()[:200]}"

    remote = _origin_remote_url(root)
    commits = _parse_git_log_numstat(result.stdout)
    proj: GitSnapshotProject = {
        "remote": canonicalize_remote_url(remote) if remote else "",
        "local_path": str(root),
        "commits": commits,
    }
    return proj, None


def _origin_remote_url(root: Path) -> str:
    """Return the `origin` remote URL or empty string on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _parse_git_log_numstat(out: str) -> list[GitCommit]:
    """Parse `git log --format=<RS>%H\\t%cI\\t%ae\\t%s --numstat` output.

    Records are RS-prefixed (\\x1e). Numstat rows follow each format line:
      <add>\\t<del>\\t<path>          # text file
      -\\t-\\t<path>                  # binary file (treated as 0/0)
      <add>\\t<del>\\t<old> => <new>  # rename (preserve <new>)

    Empty/merge commits emit a format line with no numstat rows; recorded
    as files=0, add=0, del=0.
    """
    commits: list[GitCommit] = []
    if not out:
        return commits
    # Split on the record separator. First chunk before the first \x1e is
    # empty (or whitespace) since the format string LEADS with \x1e.
    chunks = out.split("\x1e")
    for chunk in chunks:
        if not chunk.strip():
            continue
        lines = chunk.split("\n")
        header = lines[0]
        try:
            sha, date, ae, subject = header.split("\t", 3)
        except ValueError:
            continue
        files = 0
        add = 0
        rem = 0
        for ln in lines[1:]:
            if not ln.strip():
                continue
            parts = ln.split("\t", 2)
            if len(parts) < 3:
                continue
            a, d, _ = parts
            files += 1
            if a == "-":
                a = "0"
            if d == "-":
                d = "0"
            try:
                add += int(a)
                rem += int(d)
            except ValueError:
                continue
        commit: GitCommit = {
            "sha": sha,
            "date": date,
            "author_email": ae,
            "subject": subject,
            "files": files,
            "add": add,
            "del": rem,
        }
        commits.append(commit)
    return commits


# ---------------------------------------------------------------------------
# walk_session_metadata — Conductor-aware 2-level scandir walk.
# ---------------------------------------------------------------------------

_CONDUCTOR_PATTERN = re.compile(r"/conductor/workspaces/")


def walk_session_metadata(
    claude_dir: Path,
    since: datetime,  # noqa: ARG001 — kept for API stability; v=2 ignores it (see EVENTS_SCHEMA_VERSION docstring)
    *,
    deadline_monotonic: float | None = None,
) -> list[SessionsSnapshot]:
    """Walk ``<claude_dir>/projects/<encoded>/*.jsonl`` aggregating per-project
    session metadata. Returns one SessionsSnapshot row aggregating all
    projects (single row mirrors walk_git_projects' shape).

    v=2 FULL INVENTORY (Group 8). Every jsonl is counted regardless of mtime.
    The ``since`` parameter is retained for API stability but ignored — the
    aggregator picks the LATEST snapshot per (device, claude_dir) so a snap-
    shot's value is its current point-in-time view of the local sessions, not
    a delta against the prior cursor. See EVENTS_SCHEMA_VERSION docstring for
    the cross-model-review rationale that drove this change.

    `ephemeral: True` when the decoded project path matches
    `*/conductor/workspaces/*` — matched on the decoded path string, NOT
    on path existence (Conductor workspaces are routinely destroyed).

    Performance target: <500ms for 10k files via os.scandir 2-level walk.

    Codex C4: ``_read_cwd_from_latest_jsonl`` reads jsonl files line-by-line
    until a ``cwd`` field appears, so a single pathological project (no cwd
    anywhere, large jsonls) can blow the wall-clock budget. The optional
    ``deadline_monotonic`` (a ``time.monotonic()`` value) is checked at the
    top of each project iteration and aborts the scandir loop. Track 7B's
    wiring side passes the same deadline shared with walk_git_projects.
    """
    ts_now = datetime.now(timezone.utc).isoformat()
    snapshot: SessionsSnapshot = {
        "v": EVENTS_SCHEMA_VERSION,
        "type": "sessions-snapshot",
        "ts": ts_now,
        "device": "",
        "projects": [],
    }
    projects_root = claude_dir / "projects"
    if not projects_root.is_dir():
        return [snapshot]

    source_root = str(claude_dir)
    out: list[SessionMetadata] = []
    try:
        with os.scandir(projects_root) as proj_iter:
            for proj_entry in proj_iter:
                if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
                    break
                if not proj_entry.is_dir(follow_symlinks=False):
                    continue
                meta = _scan_one_project(proj_entry, source_root=source_root)
                if meta is not None:
                    out.append(meta)
    except OSError:
        return [snapshot]

    snapshot["projects"] = out
    return [snapshot]


def _scan_one_project(
    proj_entry: os.DirEntry,
    *,
    source_root: str,
) -> SessionMetadata | None:
    """One project dir → SessionMetadata. Returns None if the dir has no
    qualifying jsonl files.

    v=2 full-inventory: counts every .jsonl regardless of mtime.

    ``source_root`` is the str of the parent claude_dir passed to
    ``walk_session_metadata`` — distinguishes (device, claude_dir) tuples
    that share an encoded project name across two configured ``type:
    claude`` source roots. The aggregator keys on ``(device, source_root,
    claude_dir)`` to avoid silent overwrite on encoded-name collision."""
    sessions = 0
    total_bytes = 0
    last_mtime = 0.0
    cwd: str | None = None
    try:
        with os.scandir(proj_entry.path) as f_iter:
            for f_entry in f_iter:
                if not f_entry.name.endswith(".jsonl"):
                    continue
                if not f_entry.is_file(follow_symlinks=False):
                    continue
                try:
                    st = f_entry.stat()
                except OSError:
                    continue
                sessions += 1
                total_bytes += st.st_size
                if st.st_mtime > last_mtime:
                    last_mtime = st.st_mtime
                # Only read cwd from one file per project — first one wins.
                if cwd is None:
                    cwd = _read_cwd_from_latest_jsonl(Path(proj_entry.path))
    except OSError:
        return None
    if sessions == 0:
        return None
    decoded_path = cwd or proj_entry.name  # fallback to encoded name
    last_iso = datetime.fromtimestamp(last_mtime, tz=timezone.utc).isoformat() if last_mtime else ""
    return {
        "claude_dir": proj_entry.name,
        "source_root": source_root,
        "sessions": sessions,
        "total_kb": total_bytes // 1024,
        "last_session_at": last_iso,
        "ephemeral": bool(_CONDUCTOR_PATTERN.search(decoded_path)),
    }


# ---------------------------------------------------------------------------
# Cursor (derived) + event writer.
# ---------------------------------------------------------------------------


def last_push_ts(events_dir: Path, device_id: str) -> datetime:
    """Return the ts of the most recent ``mm-push`` event for this device.

    Reverse-scans up to ``INITIAL_CURSOR_LOOKBACK_DAYS`` daily files for the
    given device. On first run / events absent / no mm-push found, returns
    ``now - INITIAL_CURSOR_LOOKBACK_DAYS``.

    Pattern matches pullhistory.py: the log file IS the state of truth. No
    separate cursor file means no separate flock domain (A3 + A4
    elimination)."""
    default = datetime.now(timezone.utc) - timedelta(days=INITIAL_CURSOR_LOOKBACK_DAYS)
    if not events_dir.is_dir():
        return default
    today = datetime.now(timezone.utc).date()
    for delta in range(0, INITIAL_CURSOR_LOOKBACK_DAYS + 1):
        day = today - timedelta(days=delta)
        path = events_dir / f"{device_id}-{day.isoformat()}.jsonl"
        if not path.is_file():
            continue
        ts = _last_mm_push_ts(path)
        if ts is not None:
            return ts
    return default


def _last_mm_push_ts(path: Path) -> datetime | None:
    """Read `path` and return the ts of the LAST `{"type":"mm-push", ...}`
    line, or None if no such line exists. Reads forward (small daily files),
    keeping last-match semantics so the most recent push wins."""
    last: datetime | None = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") != "mm-push":
                    continue
                ts_s = obj.get("ts")
                if not isinstance(ts_s, str):
                    continue
                try:
                    last = datetime.fromisoformat(ts_s)
                except ValueError:
                    continue
    except OSError:
        return None
    return last


def write_push_event(
    events_dir: Path,
    device_id: str,
    events: list[dict],
) -> None:
    """Append `events` to today's per-device JSONL.

    Order invariant (CT-4): caller MUST construct `events` with the
    ``mm-push`` event LAST. Partial write before the mm-push appends →
    cursor doesn't advance → next push re-walks the range (deduped at
    retro render via canonical (remote, sha)). The single flock window is
    best-effort batching, NOT transactionality.

    File mode 0o600. Per-day naming: ``events/<device>-<YYYY-MM-DD>.jsonl``.
    """
    if not events:
        return
    today = datetime.now(timezone.utc).date().isoformat()
    safe_device = _safe_device_filename(device_id)
    path = events_dir / f"{safe_device}-{today}.jsonl"
    lines = [json.dumps(e, sort_keys=True).encode("utf-8") for e in events]
    fsutil.flock_append_jsonl(path, lines, mode=0o600)


_DEVICE_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_device_filename(device_id: str) -> str:
    """Conservative filename sanitizer for the per-device daily file name.
    The device_id is normally a UUID (already filename-safe), but defending
    against future schemes lets us avoid surprises if device_id becomes
    user-controlled."""
    out = _DEVICE_FILENAME_SAFE.sub("_", device_id)
    return out or "unknown"


def make_mm_push_event(
    *,
    device: str,
    mm_version: str,
    sources: list[str] | None = None,
    discovery_errors: list[str] | None = None,
    ts: datetime | None = None,
) -> MmPushEvent:
    """Construct an mm-push event row. Caller appends as the LAST element
    of the events list passed to write_push_event (CT-4).

    ``sources`` is the list of resolved source NAMES (not dicts) that
    participated in this push. ``MM_INTERNAL_SOURCE_NAMES`` are filtered
    out: ``mm-events`` is mm-owned infrastructure, not user-meaningful
    fleet activity, so it shouldn't show up in the retro skill's source
    enumeration (Codex C7). Group 8's retro skill enumerates per-source
    content stats from the synced manifest at retro time (D1D), so this
    field stays a names-only list (Codex C2: ``iter_source_diffs(skip_un
    changed=True)`` makes per-source counts unreliable on the no-content
    push path)."""
    filtered = [s for s in (sources or []) if s not in MM_INTERNAL_SOURCE_NAMES]
    return {
        "v": EVENTS_SCHEMA_VERSION,
        "type": "mm-push",
        "ts": (ts or datetime.now(timezone.utc)).isoformat(),
        "device": device,
        "mm_version": mm_version,
        "sources": filtered,
        "discovery_errors": discovery_errors or [],
    }
