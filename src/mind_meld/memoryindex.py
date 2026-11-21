"""Local qmd memory index reconciliation, refresh, state and CLI command body (Track 159A).

Keeps one local qmd index over every enabled provider's memory folders and
per-machine Codex memory copies, refreshed after each sync. Manages only
``mm-``-prefixed collections through qmd's own CLI and never edits qmd's
files directly. Never imports ``cli``.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import typer
from packaging.version import InvalidVersion, Version

from mind_meld import (
    config,
    consoles,
    core,
    lockedjson,
    manifest,
    outputstyle,
    safety,
)
from mind_meld import devices as _devices_module
from mind_meld.storage.local import LocalBackend

MIN_QMD_VERSION = Version("2.8.3")
REFRESH_DEADLINE_S = 30.0
MM_COLLECTION_PREFIX = "mm-"
_GLOBAL_INDEX = ("--index", "index")

CLAUDE_MASK = "*/memory/**/*.md"
GROK_MASK = "**/topics/**/*.md"
MUSE_MASK = "**/*.md"
CODEX_OWN_MASK = "{MEMORY.md,memory_summary.md,rollout_summaries/*.md}"
CODEX_PEER_MASK = "**/*.md"

VALID_RESULTS: frozenset[str] = frozenset({"ok", "qmd-missing", "failed", "timed-out"})

_MAX_SLUG_LEN = 64
_MAX_COLLECTION_NAME_LEN = 256
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_QMD_VERSION_RE = re.compile(r"\bv?(" + _VERSION_RE.pattern + r")([^\s]*)")
_COLLECTION_COUNT_RE = re.compile(r"Collections \(([0-9]+)\):")
_COLLECTION_LIST_RE = re.compile(r"^(.+?) \(qmd://")
_SHOW_PATH_RE = re.compile(r"^\s*Path:\s*(.+?)\s*$")
_SHOW_PATTERN_RE = re.compile(r"^\s*Pattern:\s*(.+?)\s*$")

RefreshStatus = Literal[
    "ok",
    "qmd-missing",
    "failed",
    "timed-out",
    "skipped-off",
    "skipped-running",
]


@dataclass(frozen=True)
class Collection:
    """Desired qmd collection managed by Mind Meld."""

    name: str
    path: str
    mask: str


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of a memory-index refresh attempt."""

    status: RefreshStatus
    at: str | None = None
    qmd_version: str | None = None
    collections: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemoveResult:
    """Outcome of removing mm-managed collections from qmd."""

    status: Literal["ok", "qmd-missing", "failed", "timed-out"]
    removed: tuple[str, ...] = ()


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _slug_part(raw: object) -> str:
    cleaned = safety.strip_terminal_escapes(str(raw)).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    return slug[:_MAX_SLUG_LEN].strip("-")


def _slug(raw: object, fallback: object = "") -> str:
    """Reduce a machine name to ``[a-z0-9-]`` for qmd collection identity."""
    slug = _slug_part(raw)
    if slug:
        return slug
    fb = _slug_part(fallback)
    return fb or "machine"


def _norm_path(path: str) -> str:
    try:
        return os.path.realpath(os.path.expanduser(path))
    except OSError:
        return path


def _peer_names(cfg: dict[str, Any]) -> dict[str, str]:
    """Best-effort map of device_id -> device_name from the plaintext registry."""
    try:
        storage_path = cfg.get("storage", {}).get("path")
        if not isinstance(storage_path, str) or not storage_path:
            return {}
        backend = LocalBackend(storage_path)
        out: dict[str, str] = {}
        for row in _devices_module.list_devices(backend):
            did = row.get("device_id")
            dname = row.get("device_name")
            if isinstance(did, str) and did and isinstance(dname, str) and dname:
                out[did] = dname
        return out
    except Exception:
        return {}


def desired_collections(cfg: dict[str, Any]) -> list[Collection]:
    """Return the sorted list of desired ``mm-`` qmd collections for ``cfg``."""
    resolution = config.resolve_sources(cfg, strict=False, bootstrap=False)
    collections: list[Collection] = []
    # (base_name, disambiguator_id, resolved_path, mask)
    codex_candidates: list[tuple[str, str, str, str]] = []
    seen_non_codex: set[str] = set()

    for src in resolution.selected:
        src_name = src.get("name")
        src_type = src.get("type")
        raw_path = src.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        src_path = Path(raw_path).expanduser()

        if src_name == "claude" or src_type == "claude":
            folder = src_path / "projects"
            if folder.is_dir() and "mm-claude" not in seen_non_codex:
                seen_non_codex.add("mm-claude")
                collections.append(
                    Collection(name="mm-claude", path=_norm_path(str(folder)), mask=CLAUDE_MASK)
                )
        elif src_name == "grok" or src_type == "grok":
            folder = src_path / manifest.GROK_MEMORY_ROOT
            if folder.is_dir() and "mm-grok" not in seen_non_codex:
                seen_non_codex.add("mm-grok")
                collections.append(
                    Collection(name="mm-grok", path=_norm_path(str(folder)), mask=GROK_MASK)
                )
        elif src_name == "muse" or src_type == "muse":
            folder = src_path / "memory"
            if folder.is_dir() and "mm-muse" not in seen_non_codex:
                seen_non_codex.add("mm-muse")
                collections.append(
                    Collection(name="mm-muse", path=_norm_path(str(folder)), mask=MUSE_MASK)
                )
        elif src_name == manifest.CODEX_MEMORY_SOURCE_NAME or src_type == "codex-memory":
            dir_name = manifest.codex_memory_dir_name(src_path, strict=False)
            own_folder = src_path / dir_name
            try:
                st = own_folder.lstat()
            except OSError:
                st = None
            if st is not None and stat.S_ISDIR(st.st_mode):
                dev = cfg.get("device") if isinstance(cfg.get("device"), dict) else {}
                own_id = str(dev.get("id") or "")
                own_name = dev.get("name") or ""
                base_name = f"{MM_COLLECTION_PREFIX}codex-{_slug(own_name, own_id)}"
                codex_candidates.append(
                    (base_name, own_id or "own", _norm_path(str(own_folder)), CODEX_OWN_MASK)
                )

            copies_root = Path(config.CODEX_MEMORY_COPIES_DIR).expanduser()
            try:
                if config.codex_memory_copy_dirs_missing(copies_root) == []:
                    peer_names = _peer_names(cfg)
                    for entry in sorted(copies_root.iterdir(), key=lambda p: p.name):
                        try:
                            entry_st = entry.lstat()
                        except OSError:
                            continue
                        if not stat.S_ISDIR(entry_st.st_mode):
                            continue
                        peer_id = entry.name
                        raw_name = peer_names.get(peer_id) or peer_id
                        base_name = f"{MM_COLLECTION_PREFIX}codex-{_slug(raw_name, peer_id)}"
                        codex_candidates.append(
                            (base_name, peer_id, _norm_path(str(entry)), CODEX_PEER_MASK)
                        )
            except OSError:
                pass

    counts = Counter(base for base, _did, _path, _mask in codex_candidates)
    used_names: set[str] = {c.name for c in collections}
    for base_name, dev_id, col_path, mask in codex_candidates:
        if counts[base_name] > 1:
            suffix = _slug_part(dev_id)[:8] or "id"
            final_name = f"{base_name}-{suffix}"
        else:
            final_name = base_name
        if final_name in used_names:
            idx = 2
            while f"{final_name}-{idx}" in used_names:
                idx += 1
            final_name = f"{final_name}-{idx}"
        used_names.add(final_name)
        collections.append(Collection(name=final_name, path=col_path, mask=mask))

    collections.sort(key=lambda c: c.name)
    return collections


def _run_qmd(
    qmd: str,
    args: Sequence[str],
    deadline: float,
    *,
    inspect: bool = False,
) -> tuple[
    Literal["ok", "qmd-missing", "failed", "timed-out"],
    subprocess.CompletedProcess[str] | None,
]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ("timed-out", None)
    # An explicit index bypasses QMD's project-local index discovery. Its
    # --version path opens no index, even without the flag.
    command = [qmd, *args] if args == ["--version"] else [qmd, *_GLOBAL_INDEX, *args]
    env = {**os.environ, "NO_COLOR": "1"}
    if inspect:
        # collection show only reads the YAML config. Avoid even creating the
        # default SQLite cache directory during --index argument parsing.
        env["INDEX_PATH"] = ":memory:"
    try:
        with subprocess.Popen(
            command,
            cwd="/",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        ) as proc:
            try:
                stdout, _ = proc.communicate(timeout=remaining)
            except BaseException:
                # QMD can start child processes while updating collections.
                # Stop the owned process group before releasing the refresh lock.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise
    except FileNotFoundError:
        return ("qmd-missing", None)
    except subprocess.TimeoutExpired:
        return ("timed-out", None)
    except OSError:
        return ("failed", None)
    return ("ok", subprocess.CompletedProcess(command, proc.returncode, stdout))


def _qmd_version(
    qmd: str,
    deadline: float,
) -> tuple[Literal["ok", "qmd-missing", "failed", "timed-out", "version-too-old"], str | None]:
    status, proc = _run_qmd(qmd, ["--version"], deadline)
    if status != "ok" or proc is None:
        return (status, None)
    if proc.returncode != 0:
        return ("failed", None)
    match = _QMD_VERSION_RE.search(proc.stdout)
    if match is None:
        return ("failed", None)
    ver_str = match.group(1)
    try:
        parsed = Version(ver_str + match.group(2))
    except InvalidVersion:
        return ("failed", None)
    if parsed < MIN_QMD_VERSION:
        return ("version-too-old", ver_str)
    return ("ok", ver_str)


def _list_mm_collection_names(
    qmd: str,
    deadline: float,
) -> tuple[Literal["ok", "qmd-missing", "failed", "timed-out"], list[str]]:
    status, proc = _run_qmd(qmd, ["collection", "list"], deadline)
    if status != "ok" or proc is None:
        return (status, [])
    if proc.returncode != 0:
        return ("failed", [])
    lines = proc.stdout.strip().splitlines()
    if lines and lines[0].startswith("No collections found."):
        return ("ok", [])
    count = _COLLECTION_COUNT_RE.fullmatch(lines[0]) if lines else None
    names = [match.group(1) for line in lines[1:] if (match := _COLLECTION_LIST_RE.match(line))]
    if count is None or count.group(1) != str(len(names)):
        return ("failed", [])
    return ("ok", sorted({name for name in names if name.startswith(MM_COLLECTION_PREFIX)}))


def _current_mm_collections(
    qmd: str,
    deadline: float,
) -> tuple[Literal["ok", "qmd-missing", "failed", "timed-out"], dict[str, tuple[str, str]]]:
    status, names = _list_mm_collection_names(qmd, deadline)
    if status != "ok":
        return (status, {})
    details: dict[str, tuple[str, str]] = {}
    for name in names:
        show_status, proc = _run_qmd(qmd, ["collection", "show", name], deadline, inspect=True)
        if show_status != "ok" or proc is None:
            return (show_status, {})
        if proc.returncode != 0:
            return ("failed", {})
        col_path: str | None = None
        col_pattern: str | None = None
        for line in (proc.stdout or "").splitlines():
            pm = _SHOW_PATH_RE.match(line)
            if pm:
                col_path = pm.group(1)
                continue
            patm = _SHOW_PATTERN_RE.match(line)
            if patm:
                col_pattern = patm.group(1)
        if col_path is None or col_pattern is None:
            return ("failed", {})
        details[name] = (col_path, col_pattern)
    return ("ok", details)


def _reconcile(
    qmd: str,
    desired: Sequence[Collection],
    deadline: float,
) -> tuple[Literal["ok", "qmd-missing", "failed", "timed-out"], list[str]]:
    status, current = _current_mm_collections(qmd, deadline)
    if status != "ok":
        return (status, [])

    desired_map = {c.name: c for c in desired if c.name.startswith(MM_COLLECTION_PREFIX)}

    for name in sorted(current):
        if not name.startswith(MM_COLLECTION_PREFIX):
            continue
        cur_path, cur_mask = current[name]
        wanted = desired_map.get(name)
        if (
            wanted is None
            or _norm_path(cur_path) != _norm_path(wanted.path)
            or cur_mask != wanted.mask
        ):
            rm_status, proc = _run_qmd(qmd, ["collection", "remove", name], deadline)
            if rm_status != "ok" or proc is None:
                return (rm_status, [])
            if proc.returncode != 0:
                return ("failed", [])
            del current[name]

    for name in sorted(desired_map):
        if name in current:
            continue
        wanted = desired_map[name]
        add_status, proc = _run_qmd(
            qmd,
            ["collection", "add", wanted.path, "--name", wanted.name, "--mask", wanted.mask],
            deadline,
        )
        if add_status != "ok" or proc is None:
            return (add_status, [])
        if proc.returncode != 0:
            return ("failed", [])

    return ("ok", sorted(desired_map))


def state_path() -> Path:
    """Return the local memory-index state path, derived from ``CONFIG_PATH`` at call time."""
    return config.CONFIG_PATH.parent / "memory-index.json"


def _validate_state_record(data: dict[str, Any]) -> dict[str, Any] | None:
    if data.get("version") != 1:
        return None
    result = data.get("result")
    if not isinstance(result, str) or result not in VALID_RESULTS:
        return None
    at = _parse_timestamp(data.get("at"))
    if at is None:
        return None
    qmd_version = data.get("qmd_version")
    if qmd_version is not None:
        if not isinstance(qmd_version, str) or not _VERSION_RE.fullmatch(qmd_version):
            return None
    raw_collections = data.get("collections")
    if not isinstance(raw_collections, list):
        return None
    collections: list[str] = []
    for item in raw_collections:
        if (
            not isinstance(item, str)
            or not item.startswith(MM_COLLECTION_PREFIX)
            or len(item) > _MAX_COLLECTION_NAME_LEN
        ):
            return None
        collections.append(item)
    return {
        "version": 1,
        "result": result,
        "at": at,
        "qmd_version": qmd_version,
        "collections": sorted(collections),
    }


def read_state() -> dict[str, Any]:
    """Read-only, non-blocking snapshot of ``memory-index.json``."""
    empty: dict[str, Any] = {
        "result": None,
        "at": None,
        "qmd_version": None,
        "collections": [],
    }
    path = state_path()
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return {"state": "unreadable", **empty}
    except FileNotFoundError:
        return {"state": "missing", **empty}
    except OSError:
        return {"state": "unreadable", **empty}

    with lockedjson.locked_json_snapshot(path, blocking=False) as snapshot:
        if snapshot.state != "valid":
            return {"state": snapshot.state, **empty}
        if snapshot.data == {}:
            return {"state": "missing", **empty}
        validated = _validate_state_record(snapshot.data or {})
        if validated is None:
            return {"state": "malformed", **empty}
        return {"state": "recorded", **validated}


def problem_text(result: str | None) -> tuple[str, str]:
    """Map a recorded failure or missing/malformed state to ``(problem, fix)``."""
    if result == "qmd-missing":
        return (
            f"qmd {MIN_QMD_VERSION}+ is not installed or not on PATH",
            "install qmd or run mm memory-index off",
        )
    if result == "timed-out":
        return (
            "last refresh timed out",
            "mm memory-index on",
        )
    if result == "failed":
        return (
            "last refresh failed",
            "mm memory-index on",
        )
    if result == "missing":
        return (
            "not yet refreshed",
            "mm memory-index on",
        )
    if result in ("malformed", "empty", "non_dict", "unreadable"):
        return (
            "refresh state file is unreadable",
            "mm memory-index on",
        )
    return (
        "last refresh failed",
        "mm memory-index on",
    )


def _refresh_locked(
    cfg: dict[str, Any], deadline: float, cache: lockedjson.LockedJson
) -> RefreshResult:
    status: Literal["ok", "qmd-missing", "failed", "timed-out"] = "failed"
    qmd_version = None
    previous = _validate_state_record(cache.data)
    collections = list(previous["collections"]) if previous else []
    try:
        qmd = _find_qmd()
        if qmd is None:
            status = "qmd-missing"
        else:
            version_status, qmd_version = _qmd_version(qmd, deadline)
            status = "failed" if version_status == "version-too-old" else version_status
            if status == "ok":
                status, collections = _reconcile(qmd, desired_collections(cfg), deadline)
            if status == "ok":
                status, proc = _run_qmd(qmd, ["update"], deadline)
                if proc is not None and proc.returncode != 0:
                    status = "failed"
            if status == "ok" and config.memory_index_embeddings(cfg):
                subprocess.Popen(
                    [qmd, *_GLOBAL_INDEX, "embed"],
                    cwd="/",
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
    except Exception:
        status = "failed"

    at = _now_utc()
    cache.data = {
        "version": 1,
        "result": status,
        "at": at,
        "qmd_version": qmd_version,
        "collections": collections,
    }
    cache.write_on_exit = True
    return RefreshResult(status, at, qmd_version, tuple(collections))


@contextmanager
def _index_lock() -> Iterator[lockedjson.LockedJson]:
    path = state_path()
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise OSError("memory-index state is not a regular file")
    except FileNotFoundError:
        pass
    with lockedjson.locked_json_rmw(path, on_contention="raise", retry_intervals=()) as cache:
        cache.write_on_exit = False
        yield cache


def refresh(cfg: dict[str, Any]) -> RefreshResult:
    """Reconcile the machine index after sync with one deadline and one writer."""
    if not config.memory_index_enabled(cfg):
        return RefreshResult("skipped-off")

    deadline = time.monotonic() + REFRESH_DEADLINE_S
    try:
        with _index_lock() as cache:
            # A sync can finish with a config snapshot captured before an off toggle.
            current = config.load_config()
            if not config.memory_index_enabled(current):
                return RefreshResult("skipped-off")
            result = _refresh_locked(current, deadline, cache)
        return RefreshResult("failed") if cache.write_error else result
    except lockedjson.LockContended:
        return RefreshResult("skipped-running")
    except Exception:
        return RefreshResult("failed")


def _find_qmd() -> str | None:
    # A hook's working directory is a project, so use absolute PATH entries.
    entries = [entry for entry in os.get_exec_path() if os.path.isabs(entry)]
    return shutil.which("qmd", path=os.pathsep.join(entries)) if entries else None


def remove_mm_collections() -> RemoveResult:
    """Remove every ``mm-`` collection from qmd, leaving user collections untouched."""
    deadline = time.monotonic() + REFRESH_DEADLINE_S
    qmd = _find_qmd()
    if qmd is None:
        return RemoveResult(status="qmd-missing")

    status, names = _list_mm_collection_names(qmd, deadline)
    if status != "ok":
        return RemoveResult(status=status)

    removed: list[str] = []
    for name in names:
        if not name.startswith(MM_COLLECTION_PREFIX):
            continue
        rm_status, proc = _run_qmd(qmd, ["collection", "remove", name], deadline)
        if rm_status != "ok" or proc is None:
            return RemoveResult(status=rm_status, removed=tuple(removed))
        if proc.returncode != 0:
            return RemoveResult(status="failed", removed=tuple(removed))
        removed.append(name)
    return RemoveResult(status="ok", removed=tuple(removed))


def start_detached_refresh(cfg: dict[str, Any] | None) -> bool:
    """Spawn a detached child to refresh the memory index. Never waits or prints."""
    if not config.memory_index_enabled(cfg):
        return False
    try:
        subprocess.Popen(
            [sys.executable, "-P", "-m", "mind_meld.memoryindex"],
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def _inspect_qmd(known: Sequence[str]) -> tuple[str, str | None, list[str]]:
    qmd = _find_qmd()
    if qmd is None:
        return ("qmd-missing", None, [])

    deadline = time.monotonic() + REFRESH_DEADLINE_S
    status, version = _qmd_version(qmd, deadline)
    if status != "ok":
        return (status, version, [])

    # QMD's collection list initializes the database and can rewrite index.yml.
    # Verify known mm-owned names via the read-only show path instead.
    collections: list[str] = []
    for name in known:
        probe, proc = _run_qmd(qmd, ["collection", "show", name], deadline, inspect=True)
        if probe != "ok":
            return (probe, version, [])
        if proc is not None and proc.returncode == 0:
            collections.append(name)
    return ("ok", version, collections)


def diag_state(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Build the non-secret memory-index view with read-only probes when enabled."""
    enabled = config.memory_index_enabled(cfg)
    recorded = read_state()
    qmd_version = None
    collections: list[str] = []
    if enabled:
        _, qmd_version, collections = _inspect_qmd(recorded["collections"])
    return {
        "enabled": enabled,
        "embeddings": config.memory_index_embeddings(cfg),
        "qmd_version": qmd_version,
        "collections": collections,
        "last_refresh": {key: recorded[key] for key in ("state", "result", "at")},
    }


def _print_state(cfg: dict[str, Any]) -> None:
    enabled = config.memory_index_enabled(cfg)
    embeddings = config.memory_index_embeddings(cfg)
    recorded = read_state()
    version_status, version, collections_display = _inspect_qmd(recorded["collections"])
    if version_status == "version-too-old":
        qmd_display = f"{version} (requires {MIN_QMD_VERSION}+)"
    elif version is not None:
        qmd_display = version
    else:
        qmd_display = {
            "timed-out": "timed out",
            "qmd-missing": "not found",
        }.get(version_status, "error")

    consoles.console.print("[bold]Memory index[/bold]")
    consoles.console.print(f"  enabled:       {'on' if enabled else 'off'}")
    consoles.console.print(f"  embeddings:    {'on' if embeddings else 'off'}")
    consoles.console.print(f"  qmd:           {safety.safe_str(qmd_display)}")
    if collections_display:
        rendered_cols = ", ".join(
            safety.safe_str(safety.safe_terminal_str(name)) for name in collections_display
        )
        consoles.console.print(f"  collections:   {rendered_cols}")
    else:
        consoles.console.print("  collections:   none")

    rec_state = recorded["state"]
    if rec_state == "recorded":
        res = recorded["result"] or "unknown"
        at = recorded["at"] or "unknown"
        consoles.console.print(f"  last refresh:  {safety.safe_str(res)} ({safety.safe_str(at)})")
    elif rec_state == "missing":
        consoles.console.print("  last refresh:  never")
    elif rec_state == "lock_failed":
        consoles.console.print("  last refresh:  running")
    else:
        consoles.console.print(f"  last refresh:  {safety.safe_str(rec_state)}")


def memory_index(action: str | None = None) -> None:
    """Body of ``mm memory-index [on|off]``."""
    if action not in (None, "on", "off"):
        core._print_error(
            f"unknown memory-index action '{safety.safe_terminal_str(action)}' "
            "(expected 'on' or 'off').",
            fix="mm memory-index --help",
        )
        raise typer.Exit(2)

    if action is None:
        _print_state(core._get_config(read_only=True))
        return

    core._get_config(read_only=False)
    if action == "on":
        _enable_index()
    else:
        _disable_index()


def _set_enabled(enabled: bool) -> None:
    config.patch_config_on_disk({"memory_index": {"enabled": enabled}})


def _enable_index() -> None:
    deadline = time.monotonic() + REFRESH_DEADLINE_S
    qmd = _find_qmd()
    ver_status, ver = _qmd_version(qmd, deadline) if qmd else ("qmd-missing", None)
    if ver_status != "ok":
        detail = f"found {ver}" if ver is not None else ver_status
        core._error(
            f"could not verify qmd {MIN_QMD_VERSION}+ ({detail}).",
            fix="install qmd (2.8.3+) and re-run mm memory-index on",
        )

    try:
        with _index_lock() as cache:
            _set_enabled(True)
            res = _refresh_locked(config.load_config(), deadline, cache)
    except lockedjson.LockContended:
        core._error("memory index is busy.", fix="mm memory-index on")
        raise
    except Exception:
        core._error("could not enable the memory index.", fix="mm memory-index on")
        raise

    if res.status != "ok" or cache.write_error:
        problem, fix = problem_text(res.status if not cache.write_error else "unreadable")
        core._error(f"memory index enabled, but {problem}.", fix=fix)

    count = len(res.collections)
    noun = "collection" if count == 1 else "collections"
    core._print_outcome(
        outputstyle._outcome_line(
            "memory-index", f"enabled on this device ({count} {noun} indexed)"
        )
    )
    consoles.console.print(
        outputstyle._fix_line("mm memory-index off"),
        markup=False,
        highlight=False,
        soft_wrap=True,
    )


def _disable_index() -> None:
    try:
        with _index_lock() as cache:
            was_enabled = config.memory_index_enabled(config.load_config())
            if was_enabled:
                _set_enabled(False)
            rm = remove_mm_collections()
            if rm.status == "ok":
                # Keep the inode used as a lock and invalidate the former success.
                cache.data = {}
                cache.write_on_exit = True
    except lockedjson.LockContended:
        core._error("memory index is busy.", fix="mm memory-index off")
        raise
    except Exception:
        core._error("could not disable the memory index.", fix="mm memory-index off")
        raise

    if rm.status == "qmd-missing":
        if not was_enabled:
            core._print_outcome(
                outputstyle._outcome_line("memory-index", "already off on this device")
            )
        else:
            core._notice("qmd is not on PATH; existing mm- collections could not be removed.")
            core._print_outcome(
                outputstyle._outcome_line("memory-index", "disabled on this device")
            )
        return

    if rm.status != "ok":
        core._error(
            f"memory index disabled, but removing mm- collections failed ({rm.status}).",
            fix="mm memory-index off",
        )

    if not was_enabled and not rm.removed:
        core._print_outcome(outputstyle._outcome_line("memory-index", "already off on this device"))
        return

    count = len(rm.removed)
    detail = (
        f"disabled on this device ({count} {'collection' if count == 1 else 'collections'} removed)"
        if count
        else "disabled on this device"
    )
    core._print_outcome(outputstyle._outcome_line("memory-index", detail))


def _detached_main() -> int:
    try:
        cfg = core._get_config(read_only=True)
        refresh(cfg)
    except (Exception, typer.Exit):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(_detached_main())
