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
import stat
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Sequence
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

CLAUDE_MASK = "*/memory/**/*.md"
GROK_MASK = "**/topics/**/*.md"
MUSE_MASK = "**/*.md"
CODEX_OWN_MASK = "{MEMORY.md,memory_summary.md,rollout_summaries/*.md}"
CODEX_PEER_MASK = "**/*.md"

VALID_RESULTS: frozenset[str] = frozenset({"ok", "qmd-missing", "failed", "timed-out"})

_MAX_SLUG_LEN = 64
_MAX_COLLECTION_NAME_LEN = 256
_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")
_COLLECTION_LIST_RE = re.compile(r"^(\S+)\s+\(qmd://")
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
) -> tuple[
    Literal["ok", "qmd-missing", "failed", "timed-out"],
    subprocess.CompletedProcess[str] | None,
]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ("timed-out", None)
    try:
        proc = subprocess.run(
            [qmd, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except FileNotFoundError:
        return ("qmd-missing", None)
    except subprocess.TimeoutExpired:
        return ("timed-out", None)
    except OSError:
        return ("failed", None)
    return ("ok", proc)


def _qmd_version(
    qmd: str,
    deadline: float,
) -> tuple[Literal["ok", "qmd-missing", "failed", "timed-out", "version-too-old"], str | None]:
    status, proc = _run_qmd(qmd, ["--version"], deadline)
    if status != "ok" or proc is None:
        return (status, None)
    if proc.returncode != 0:
        return ("failed", None)
    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    match = _VERSION_RE.search(combined)
    if match is None:
        return ("failed", None)
    ver_str = match.group(1)
    try:
        parsed = Version(ver_str)
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
    names: list[str] = []
    for line in (proc.stdout or "").splitlines():
        match = _COLLECTION_LIST_RE.match(line.strip())
        if match:
            name = match.group(1)
            if name.startswith(MM_COLLECTION_PREFIX):
                names.append(name)
    return ("ok", sorted(set(names)))


def _current_mm_collections(
    qmd: str,
    deadline: float,
) -> tuple[Literal["ok", "qmd-missing", "failed", "timed-out"], dict[str, tuple[str, str]]]:
    status, names = _list_mm_collection_names(qmd, deadline)
    if status != "ok":
        return (status, {})
    details: dict[str, tuple[str, str]] = {}
    for name in names:
        show_status, proc = _run_qmd(qmd, ["collection", "show", name], deadline)
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


def refresh(cfg: dict[str, Any]) -> RefreshResult:
    """Reconcile ``mm-`` collections and run ``qmd update`` (plus background ``qmd embed``)."""
    if not config.memory_index_enabled(cfg):
        return RefreshResult(status="skipped-off")

    deadline = time.monotonic() + REFRESH_DEADLINE_S
    try:
        with lockedjson.locked_json_rmw(
            state_path(), on_contention="raise", retry_intervals=()
        ) as cache:
            record_result: Literal["ok", "qmd-missing", "failed", "timed-out"] = "failed"
            qmd_version: str | None = None
            collections: list[str] = []

            qmd = shutil.which("qmd")
            if qmd is None:
                record_result = "qmd-missing"
            else:
                ver_status, qmd_version = _qmd_version(qmd, deadline)
                if ver_status == "qmd-missing":
                    record_result = "qmd-missing"
                elif ver_status == "timed-out":
                    record_result = "timed-out"
                elif ver_status != "ok":
                    record_result = "failed"
                else:
                    desired = desired_collections(cfg)
                    rec_status, collections = _reconcile(qmd, desired, deadline)
                    if rec_status != "ok":
                        record_result = rec_status
                    else:
                        upd_status, upd_proc = _run_qmd(qmd, ["update"], deadline)
                        if upd_status != "ok":
                            record_result = upd_status
                        elif upd_proc is None or upd_proc.returncode != 0:
                            record_result = "failed"
                        else:
                            record_result = "ok"
                            if config.memory_index_embeddings(cfg):
                                try:
                                    subprocess.Popen(
                                        [qmd, "embed"],
                                        stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                        start_new_session=True,
                                    )
                                except Exception:
                                    record_result = "failed"

            at = _now_utc()
            sorted_cols = sorted(collections)
            cache.data = {
                "version": 1,
                "result": record_result,
                "at": at,
                "qmd_version": qmd_version,
                "collections": sorted_cols,
            }
            return RefreshResult(
                status=record_result,
                at=at,
                qmd_version=qmd_version,
                collections=tuple(sorted_cols),
            )
    except lockedjson.LockContended:
        return RefreshResult(status="skipped-running")
    except Exception:
        return RefreshResult(status="failed")


def remove_mm_collections(cfg: dict[str, Any] | None = None) -> RemoveResult:
    """Remove every ``mm-`` collection from qmd, leaving user collections untouched."""
    del cfg
    deadline = time.monotonic() + REFRESH_DEADLINE_S
    qmd = shutil.which("qmd")
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
            [sys.executable, "-m", "mind_meld.memoryindex"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def diag_state(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Build the non-secret ``memory_index`` section for ``mm diag``.

    When ``cfg`` is ``None`` or the index is disabled, runs no subprocess and
    reports version/collections from state alone (null/empty when absent).
    """
    enabled = config.memory_index_enabled(cfg)
    embeddings = config.memory_index_embeddings(cfg)
    recorded = read_state()
    last_refresh = {
        "state": recorded["state"],
        "result": recorded["result"],
        "at": recorded["at"],
    }

    if not enabled:
        return {
            "enabled": False,
            "embeddings": embeddings,
            "qmd_version": None,
            "collections": [],
            "last_refresh": last_refresh,
        }

    qmd_version: str | None = recorded["qmd_version"]
    collections: list[str] = list(recorded["collections"])

    try:
        deadline = time.monotonic() + REFRESH_DEADLINE_S
        qmd = shutil.which("qmd")
        if qmd is None:
            qmd_version = None
            collections = []
        else:
            ver_status, probed_ver = _qmd_version(qmd, deadline)
            if ver_status in ("ok", "version-too-old"):
                qmd_version = probed_ver
            else:
                qmd_version = None
            col_status, probed_cols = _list_mm_collection_names(qmd, deadline)
            if col_status == "ok":
                collections = probed_cols
            else:
                collections = []
    except Exception:
        qmd_version = None
        collections = []

    return {
        "enabled": True,
        "embeddings": embeddings,
        "qmd_version": qmd_version,
        "collections": collections,
        "last_refresh": last_refresh,
    }


def _print_state(cfg: dict[str, Any]) -> None:
    enabled = config.memory_index_enabled(cfg)
    embeddings = config.memory_index_embeddings(cfg)
    recorded = read_state()

    qmd_display = "not found"
    collections_display: list[str] = []
    deadline = time.monotonic() + REFRESH_DEADLINE_S
    qmd = shutil.which("qmd")
    if qmd is not None:
        ver_status, ver = _qmd_version(qmd, deadline)
        if ver_status == "ok" and ver:
            qmd_display = ver
        elif ver_status == "version-too-old" and ver:
            qmd_display = f"{ver} (requires {MIN_QMD_VERSION}+)"
        elif ver_status == "timed-out":
            qmd_display = "timed out"
        elif ver_status == "qmd-missing":
            qmd_display = "not found"
        else:
            qmd_display = "error"

        col_status, probed_cols = _list_mm_collection_names(qmd, deadline)
        if col_status == "ok":
            collections_display = probed_cols

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
        core._error(
            f"unknown memory-index action '{safety.safe_terminal_str(action)}' "
            "(expected 'on' or 'off').",
            fix="mm memory-index --help",
        )

    if action is None:
        cfg = core._get_config(read_only=True)
        _print_state(cfg)
        return

    cfg = core._get_config(read_only=False)

    if action == "on":
        deadline = time.monotonic() + REFRESH_DEADLINE_S
        qmd = shutil.which("qmd")
        if qmd is None:
            core._error(
                f"qmd {MIN_QMD_VERSION}+ is not installed or not on PATH.",
                fix="install qmd (2.8.3+) and re-run mm memory-index on",
            )
        ver_status, ver = _qmd_version(qmd, deadline)
        if ver_status == "version-too-old":
            core._error(
                f"qmd {ver} is too old; Mind Meld requires qmd {MIN_QMD_VERSION}+.",
                fix="upgrade qmd to 2.8.3+ and re-run mm memory-index on",
            )
        if ver_status != "ok":
            core._error(
                f"could not verify qmd {MIN_QMD_VERSION}+ ({ver_status}).",
                fix="install qmd (2.8.3+) and re-run mm memory-index on",
            )

        config.patch_config_on_disk({"memory_index": {"enabled": True}})
        cfg.setdefault("memory_index", {})["enabled"] = True

        res = refresh(cfg)
        if res.status == "ok":
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
            return
        if res.status == "skipped-running":
            core._print_outcome(
                outputstyle._outcome_line(
                    "memory-index", "enabled on this device (refresh already running)"
                )
            )
            consoles.console.print(
                outputstyle._fix_line("mm memory-index off"),
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
            return

        problem, fix = problem_text(res.status)
        core._error(f"memory index enabled, but {problem}.", fix=fix)

    # action == "off"
    was_enabled = config.memory_index_enabled(cfg)
    config.patch_config_on_disk({"memory_index": {"enabled": False}})
    cfg.setdefault("memory_index", {})["enabled"] = False

    rm = remove_mm_collections(cfg)
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
