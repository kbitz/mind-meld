"""Status and diagnostic inspection, independent of CLI registration."""

from __future__ import annotations

import io
import json
import re
import stat
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import typer
from packaging.version import InvalidVersion, Version

from mind_meld import (
    __version__,
    attemptlog,
    core,
    crypto,
    events,
    events_tail,
    host_registry,
    initflow,
    manifest,
    projectid,
    resolveflow,
    safety,
    seen_sources,
    sidecar,
    skill_link,
    statedb,
    syncdiag,
    syncthing,
    upgrade,
)
from mind_meld import config as _config_module
from mind_meld.config import (
    DEFAULT_SOURCES,
    DEFAULT_STORAGE_PATH,
    MM_INTERNAL_SOURCE_NAMES,
    get_sources,
    load_config,
    resolve_sources,
)
from mind_meld.consoles import console
from mind_meld.crypto import (
    fetch_crypto_init,
    root_salt_fingerprint,
)
from mind_meld.devices import inventory_devices
from mind_meld.errors import (
    CURSOR_SQLITE_URL,
    KEYRING_UNAVAILABLE_URL,
    RETIRE_MACHINE_URL,
    SQLITE_UNAVAILABLE_REMEDY,
    MindMeldError,
    SnapshotError,
)
from mind_meld.manifest import (
    build_manifest_v2,
)
from mind_meld.safety import safe_str
from mind_meld.storage import get_backend
from mind_meld.storage.keys import (
    DATA_PREFIX,
    MANIFESTS_PREFIX,
    manifest_key,
)
from mind_meld.storage.local import LocalBackend

_BROKEN_DIAG_STATUS_TO_INSTALL = {
    "foreign-dangling": "foreign",
    "error": "failed",
}


def _render_broken_skill_status(row: dict) -> str:
    """Render the first broken ``mm diag`` row through ``render_skill_status``."""
    key = row.get("key")
    if not isinstance(key, str) or not key:
        return safe_str(str(row.get("agent", "")))
    try:
        descriptor = skill_link._descriptor_for(key)
    except KeyError:
        return safe_str(str(row.get("agent", "") or key))
    raw_status = str(row.get("status") or "failed")
    mapped = _BROKEN_DIAG_STATUS_TO_INSTALL.get(raw_status, raw_status)
    store = row.get("store")
    result = skill_link.SkillInstallResult(
        descriptor,
        mapped,  # type: ignore[arg-type]
        link_target=Path(store) if isinstance(store, str) and store else None,
        reason=str(row["detail"]) if row.get("detail") else None,
    )
    return skill_link.render_skill_status(result)


def _peer_version_remedy(version: object) -> str:
    """The command that updates a peer, by the version it last pushed with."""
    try:
        if isinstance(version, str) and Version(version) >= Version("1.3.0"):
            return "mm update"
    except InvalidVersion:
        pass
    return upgrade.INSTALL_CMD


def _project_name_lines(
    backend: LocalBackend,
    devices: list[dict],
    device_id: str,
    passphrase: str,
    memory_kb: int,
    *,
    manifests: dict[str, dict | None] | None = None,
) -> list[str]:
    """The project-name lines `mm status` prints after its verdict (Track 90A).

    In table order: a home that does not resolve, a Claude folder naming that
    stopped decoding, one row per peer whose another-home names are withheld
    as old form, the retire pointer, and the projects with no checkout here.
    Peer text goes through `safety`; an unreadable or vanished peer is skipped;
    nothing is persisted. A same-home older peer has nothing withheld and
    prints nothing.
    """
    lines: list[str] = []
    failure = projectid.home_failure()
    if failure is not None:
        return [
            "Project memory syncs by path: could not resolve this machine's home folder "
            f"({safety.safe_terminal_str(failure)}). Sync continues. Check $HOME, then mm diag."
        ]
    if not projectid.enabled():
        return lines
    if projectid.encoding_suspect():
        lines.append(
            "Project memory syncs by path: no Claude project folder here matches a "
            "directory on this machine, so Claude's folder naming may have changed. "
            "Sync continues and nothing is lost. Run: mm update; if it stays, send the "
            "output of ls ~/.claude/projects."
        )
    peers = [d for d in devices if d.get("device_id") != device_id]
    if manifests is None:
        manifests = {}
        for peer in peers:
            try:
                fetch = core._fetch_remote_manifest(
                    backend, peer["device_id"], passphrase, memory_kb
                )
            except (OSError, MindMeldError):
                continue
            if fetch.is_ok:
                manifests[peer["device_id"]] = fetch.manifest
    report = core.project_name_report(manifests)
    by_id = {d["device_id"]: d for d in peers}
    for did, files in sorted(report.old_form.items(), key=lambda item: str(item[0])):
        device = by_id.get(did)
        if device is None or not files:
            continue
        name = safety.safe_terminal_str(device.get("device_name", did))
        version = device.get("last_seen_version")
        last = device.get("last_seen")
        if isinstance(version, str) and version:
            seen = f", last push {safety.safe_terminal_str(str(last)[:10])}" if last else ""
            where = f"{name} ({safety.safe_terminal_str(version)}{seen})"
        else:
            where = f"{name} (version unknown)"
        lines.append(
            f"{core._count(files, 'file', 'files')} from {name} "
            f"{'waits' if files == 1 else 'wait'} until it publishes "
            f"shared names. On {where}: {_peer_version_remedy(version)}, then mm push"
        )
    if report.old_form:
        lines.append(f"If one of these machines is gone for good: {RETIRE_MACHINE_URL}")
    if report.not_placed:
        lines.append(
            f"Not placed here: {core._count(report.not_placed_files, 'file', 'files')} in "
            f"{core._count(report.not_placed_projects, 'project', 'projects')} with no known "
            "checkout (list: mm pull --dry-run --verbose)."
        )
    return [safe_str(line) for line in lines]


def _status_verdict(
    *,
    source: str | None,
    blocking: str | None,
    conflicts: int,
    projection: dict,
    counts: tuple[int, int, int],
    metadata: bool,
    first_push: bool,
    capture_action: str | None = None,
    manifest_arriving: bool = False,
    names_ready: bool = False,
    names_held: bool = False,
    left_out: str = "",
    peers_overdue: str = "",
) -> str:
    """Pure priority selector over explicit current observations and run evidence."""
    scope = f"Source '{source}' — " if source is not None else ""
    if blocking:
        verdict = blocking
    elif conflicts:
        verdict = f"{conflicts} unresolved conflicts. Run: mm resolve"
    else:
        verdict = ""
        for verb in ("pull", "push"):
            state = projection[verb]
            selected = state["selected"]
            if state["unknown_reason"]:
                reason = state["unknown_reason"]
                recovery = (
                    "Retry mm status once; if still busy, run: mm diag"
                    if reason == "contended"
                    else "Run: mm diag"
                )
                verdict = f"Sync run evidence unknown [run-evidence-{reason}] ({verb}). {recovery}"
                break
            if state["unresolved"]:
                cause = selected["cause"] if selected else "unknown"
                remedy = f"mm {verb}" if cause in ("ok", "no-changes") else "mm diag"
                verdict = (
                    f"Latest {verb} needs attention ({cause}); recovery is unproven. Run: {remedy}"
                )
                break
        if not verdict:
            if capture_action:
                verdict = capture_action
            elif manifest_arriving:
                verdict = "This device's manifest is still arriving from sync. Run: mm status"
            elif first_push:
                verdict = "This device has not pushed yet. Run: mm push"
            elif any(counts):
                verdict = (
                    f"{counts[0]} new, {counts[1]} modified and "
                    f"{counts[2]} deleted waiting to push. Run: mm push"
                )
            elif names_ready:
                verdict = "Project memory ready to publish under shared names. Run: mm push"
            elif metadata:
                verdict = "Metadata-only changes waiting to push. Run: mm push"
            elif names_held:
                verdict = (
                    "Project memory not published until Claude's projects folder can be listed."
                )
            elif left_out:
                verdict = f"Not everything is published: {left_out}"
            elif peers_overdue:
                verdict = peers_overdue
            else:
                verdict = "In sync with this device's last push."
    return safety.safe_terminal_str(scope + verdict)


PEER_WAIT_WARN = timedelta(hours=24)


@dataclass(frozen=True)
class PeerState:
    """One peer's push and applied state for ``mm status`` (Track 92A)."""

    device_id: str
    device_name: str
    state: str
    last_push: str | None = None
    since: datetime | None = None
    overdue: bool = False
    remedy: str | None = None
    waiting: int = 0
    newer_version: int | None = None
    cause: str | None = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)


def _parse_aware_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None and raw.utcoffset() is not None else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return dt
    except (TypeError, ValueError, OverflowError):
        return None


def _dt_to_us(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return (dt - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(microseconds=1)


def _peer_states(
    devices: Sequence[dict],
    device_id: str,
    fetches: dict[str, Any],
    cursors: dict[str, statedb.CursorRow] | str | BaseException | None,
    syncthing_state: Any = None,
    now: datetime | None = None,
    *,
    cursors_cause: str | None = None,
) -> list[PeerState]:
    """Pure per-peer push and applied-state classifier (Track 92A)."""
    ref_now = now if now is not None else datetime.now(timezone.utc)
    cursors_unknown = not isinstance(cursors, dict)
    if cursors_unknown:
        if isinstance(cursors, str) and cursors:
            db_cause = cursors
        elif isinstance(cursors, BaseException):
            db_cause = core.ledger_cause(cursors) or "unreadable"
        else:
            db_cause = cursors_cause or "unreadable"
        cursors_map: dict[str, statedb.CursorRow] = {}
    else:
        db_cause = None
        cursors_map = cursors

    results: list[PeerState] = []
    for entry in devices:
        pid = entry.get("device_id")
        if not isinstance(pid, str) or not pid or pid == device_id:
            continue
        pname = str(entry.get("device_name") or pid)
        raw_last_seen = entry.get("last_seen")
        last_seen_str = (
            raw_last_seen.strip()
            if isinstance(raw_last_seen, str) and raw_last_seen.strip()
            else None
        )
        fetch = fetches.get(pid)

        if fetch is None or fetch == "unreadable" or isinstance(fetch, BaseException):
            newer_ver = getattr(fetch, "newer_version", None)
            if isinstance(newer_ver, int):
                results.append(
                    PeerState(
                        device_id=pid,
                        device_name=pname,
                        state="newer-format",
                        last_push=last_seen_str,
                        newer_version=newer_ver,
                    )
                )
            else:
                results.append(
                    PeerState(
                        device_id=pid,
                        device_name=pname,
                        state="unreadable",
                        last_push=last_seen_str,
                    )
                )
            continue

        if isinstance(fetch, dict):
            fetch_status = "ok"
            manifest_dict: dict | None = fetch
            newer_ver = None
        else:
            fetch_status = getattr(fetch, "status", "corrupt")
            manifest_dict = getattr(fetch, "manifest", None)
            newer_ver = getattr(fetch, "newer_version", None)

        if isinstance(newer_ver, int):
            results.append(
                PeerState(
                    device_id=pid,
                    device_name=pname,
                    state="newer-format",
                    last_push=last_seen_str,
                    newer_version=newer_ver,
                )
            )
            continue

        if fetch_status == "corrupt":
            results.append(
                PeerState(
                    device_id=pid,
                    device_name=pname,
                    state="unreadable",
                    last_push=last_seen_str,
                )
            )
            continue

        if fetch_status == "missing" or not isinstance(manifest_dict, dict):
            if last_seen_str is not None:
                since_dt = _parse_aware_dt(last_seen_str)
                is_overdue = bool(since_dt is not None and (ref_now - since_dt) > PEER_WAIT_WARN)
                results.append(
                    PeerState(
                        device_id=pid,
                        device_name=pname,
                        state="not-arrived",
                        last_push=last_seen_str,
                        since=since_dt,
                        overdue=is_overdue,
                        remedy="mm diag",
                    )
                )
            else:
                results.append(
                    PeerState(
                        device_id=pid,
                        device_name=pname,
                        state="never-pushed",
                    )
                )
            continue

        raw_m_ts = manifest_dict.get("timestamp")
        manifest_ts_str = (
            raw_m_ts.strip() if isinstance(raw_m_ts, str) and raw_m_ts.strip() else None
        )
        last_push = last_seen_str if last_seen_str is not None else manifest_ts_str
        manifest_dt = _parse_aware_dt(manifest_ts_str)
        manifest_us = _dt_to_us(manifest_dt)

        if cursors_unknown:
            results.append(
                PeerState(
                    device_id=pid,
                    device_name=pname,
                    state="unknown",
                    last_push=last_push,
                    remedy="mm diag",
                    cause=db_cause,
                )
            )
            continue

        cursor = cursors_map.get(pid)
        applied_us = cursor[0] if cursor is not None else None
        seen_us = cursor[1] if cursor is not None else None
        waiting_count = int(cursor[2]) if cursor is not None else 0
        identity = core._manifest_cursor_hash(manifest_dict)
        applied_hashes = cursor[3:5] if cursor is not None else ()
        seen_hash = cursor[5] if cursor is not None and len(cursor) > 5 else None

        if (
            applied_us is not None
            and manifest_us is not None
            and applied_us >= manifest_us
            and identity in applied_hashes
        ):
            results.append(
                PeerState(
                    device_id=pid,
                    device_name=pname,
                    state="applied",
                    last_push=last_push,
                )
            )
        elif (
            waiting_count > 0
            and seen_hash == identity
            and (seen_us is None or manifest_us is None or seen_us >= manifest_us)
        ):
            is_overdue = bool(manifest_dt is not None and (ref_now - manifest_dt) > PEER_WAIT_WARN)
            results.append(
                PeerState(
                    device_id=pid,
                    device_name=pname,
                    state="waiting",
                    last_push=last_push,
                    since=manifest_dt,
                    overdue=is_overdue,
                    remedy="mm diag",
                    waiting=waiting_count,
                )
            )
        else:
            is_overdue = bool(manifest_dt is not None and (ref_now - manifest_dt) > PEER_WAIT_WARN)
            results.append(
                PeerState(
                    device_id=pid,
                    device_name=pname,
                    state="not-pulled",
                    last_push=last_push,
                    since=manifest_dt,
                    overdue=is_overdue,
                    remedy="mm pull",
                )
            )

    return results


def _overdue_peers_line(peer_states: Sequence[PeerState]) -> str:
    overdue = [p for p in peer_states if p.overdue and p.since is not None]
    if not overdue:
        return ""
    first = overdue[0]
    assert first.since is not None
    name = safety.safe_terminal_str(first.device_name)
    day = first.since.astimezone(timezone.utc).strftime("%Y-%m-%d")
    more = f" (and {len(overdue) - 1} more)" if len(overdue) > 1 else ""
    remedy = first.remedy or "mm pull"
    return safety.safe_terminal_str(
        f"Waiting more than 24 h for {name}'s push of {day}{more}. Run: {remedy}"
    )


def _format_last_push(raw: str | None) -> str:
    dt = _parse_aware_dt(raw)
    if dt is not None:
        return f"last push {dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    if raw:
        return f"last push {safety.safe_terminal_str(raw)}"
    return "last push unknown"


def _format_peer_state(p: PeerState) -> str:
    if p.state == "never-pushed":
        return "never pushed"
    if p.state == "newer-format":
        return safety.safe_terminal_str(core._newer_format_message(p.newer_version or 0))
    if p.state == "unreadable":
        prefix = f"{_format_last_push(p.last_push)}; " if p.last_push else ""
        return f"{prefix}manifest unreadable here (mm pull shows why)"
    if p.state == "unknown":
        prefix = f"{_format_last_push(p.last_push)}; " if p.last_push else ""
        cause = safety.safe_terminal_str(p.cause or "unreadable")
        return f"{prefix}applied state unknown (state.db {cause}). Run: mm diag"
    push_str = _format_last_push(p.last_push)
    if p.state == "applied":
        return f"{push_str}; applied here"
    if p.state == "not-pulled":
        return f"{push_str}; not pulled here yet"
    if p.state == "waiting":
        return f"{push_str}; {core._count(p.waiting, 'file', 'files')} waiting for sync"
    if p.state == "not-arrived":
        return f"{push_str}; its manifest has not arrived"
    return push_str


def _syncthing_status_lines(st: syncthing.SyncthingState, storage_path: Path) -> list[str]:
    lines: list[str] = []
    if st.state == "not-found":
        return ["  Syncthing: not found on this machine"]
    if st.state == "config-unreadable":
        where = core._home_relative_path(st.config_dir) if st.config_dir else "config.xml"
        return [f"  Syncthing: config unreadable at {safety.safe_terminal_str(where)}"]
    if st.state == "address-unsupported":
        addr = safety.safe_terminal_str(st.gui_address or "unknown")
        lines.append(f"  Syncthing: unsupported GUI address {addr}")
    elif st.state in ("unreachable", "unauthorized", "error"):
        addr = safety.safe_terminal_str(st.gui_address or "unknown")
        reason = "unauthorized" if st.state == "unauthorized" else (st.cause or st.state)
        lines.append(f"  Syncthing: not reachable at {addr} ({safety.safe_terminal_str(reason)})")
    elif "no-folder" in st.problems:
        shown_path = safety.safe_terminal_str(core._home_relative_path(storage_path))
        lines.append(f"  Syncthing: no Syncthing folder shares {shown_path}")
    else:
        if st.total_peers is not None and st.connected_peers is not None:
            peer_part = f"connected {st.connected_peers} of {st.total_peers} devices"
            if st.disconnected_peers:
                names = ", ".join(safety.safe_terminal_str(n) for n in st.disconnected_peers)
                peer_part += f" ({names} disconnected)"
        elif st.total_peers is not None:
            peer_part = f"{st.total_peers} devices configured"
        else:
            peer_part = "connected"

        if st.folder_paused:
            state_part = "paused"
        elif st.folder_state == "idle" and st.files_to_fetch == 0 and st.pull_errors == 0:
            state_part = "up to date"
        elif st.folder_state:
            state_part = safety.safe_terminal_str(st.folder_state)
        else:
            state_part = "state unknown"
        if st.files_to_fetch is not None and (st.files_to_fetch > 0 or state_part == "syncing"):
            state_part += f", {core._count(st.files_to_fetch, 'file', 'files')} to fetch"
        if st.pull_errors is not None and st.pull_errors > 0:
            state_part += (
                f", {core._count(st.pull_errors, 'pull error', 'pull errors')}. Run: mm diag"
            )
        lines.append(f"  Syncthing: {peer_part}; {state_part}")

    setting_problems = [p for p in st.problems if p != "no-folder"]
    if st.state == "ok" and setting_problems:
        lines.append(
            f"  Syncthing folder settings need attention ({len(setting_problems)}). Run: mm diag"
        )
    return [safe_str(line) for line in lines]


def _backend_storage_path(backend: object, config: dict | None = None) -> Path:
    root = getattr(backend, "root", None)
    if isinstance(root, (str, Path)) and str(root):
        return Path(root).expanduser()
    if isinstance(config, dict):
        storage = config.get("storage")
        if isinstance(storage, dict):
            raw = storage.get("path")
            if isinstance(raw, (str, Path)) and str(raw):
                return Path(raw).expanduser()
    return Path(DEFAULT_STORAGE_PATH).expanduser()


@contextmanager
def _status_notices():
    """Tightly scoped post-auth collection, restored before any fatal replay."""
    out, err, report = io.StringIO(), io.StringIO(), {}
    try:
        with redirect_stdout(out), redirect_stderr(err):
            yield report
    except BaseException as exc:
        # Known failures arrive as data, never parsed Rich output.
        failure = report.get("failure")
        print(
            failure or f"Status inspection failed ({type(exc).__name__}).",
            file=sys.stderr,
            flush=True,
        )
        notices = bool(err.getvalue() or out.getvalue())
        sys.stderr.write(err.getvalue())
        sys.stdout.write(out.getvalue())
        sys.stdout.flush()
        if failure:
            # Preserve the specific remedy after any intervening notices.
            if notices:
                print(failure, file=sys.stderr, flush=True)
        else:
            print("Inspection did not complete. Run: mm diag", file=sys.stderr, flush=True)
        raise
    else:
        print(report["verdict"], flush=True)
        sys.stdout.write(out.getvalue())
        sys.stdout.flush()
        sys.stderr.write(err.getvalue())
        sys.stderr.flush()


def status(
    source: str | None = typer.Option(
        None, "--source", help="Show status for a specific source only"
    ),
) -> None:
    """Compare selected local context with this device's last push; verdict after authentication."""
    config = core._get_config(read_only=True)
    passphrase = core._get_passphrase_or_exit()
    device_id = config["device"]["id"]
    device_name = config["device"]["name"]
    max_file_size = config["sync"]["max_file_size"]

    with _status_notices() as report:
        backend = get_backend(config)
        pending: list[str] = []
        try:
            memory_kb = core._init_crypto_session(
                backend, passphrase, config, read_only=True, pending=pending
            )
        except MindMeldError as e:
            report["failure"] = "Error: " + safety.safe_terminal_str(str(e))
            raise typer.Exit(1) from e
        for note in pending:
            console.print(safe_str(note))
        fetch = core._fetch_remote_manifest(backend, device_id, passphrase, memory_kb)
        remote_manifest = fetch.manifest if fetch.is_ok else None
        if remote_manifest is not None:
            # Retired host entries must not manufacture a pending-selection
            # verdict. Deliberately disabled active sources retain that signal.
            remote_manifest = core._filter_disabled_sources(remote_manifest, [])
        source_resolution = core._retain_prior_default_sources(
            resolve_sources(config, strict=True, bootstrap=False), remote_manifest, config
        )
        sources_configs = source_resolution.available
        selected = source_resolution.selected
        selected_names = {s["name"] for s in selected}
        available_names = {s["name"] for s in sources_configs}
        blocking = None
        if source is not None and source not in selected_names:
            blocking = "Requested source is unknown or disabled. Run: mm sources"
        elif source is not None and source not in available_names:
            blocking = "Requested source is unavailable. Restore its root, then run: mm status"
        elif not sources_configs:
            blocking = "No usable sync sources. Run: mm sources"
        for src in selected:
            if source is not None and src["name"] != source:
                continue
            if src["name"] not in available_names and src["name"] in (remote_manifest or {}).get(
                "sources", {}
            ):
                blocking = (
                    f"Previously published source '{src['name']}' is unavailable. "
                    "Restore its root, then run: mm status"
                )
        capture_rows = _read_capture_rows(sources_configs, device_id)
        comparison_sources = [
            src for src in sources_configs if source is None or src["name"] == source
        ]
        nested_roots: dict[str, set[str]] = {}
        # Strict enumeration/hash failures cannot manufacture a healthy empty scan.
        # This may reread event bytes; it does not add another EventScan parse.
        comparison_complete = True
        left_out_clause = ""
        try:
            local_manifest = build_manifest_v2(
                device_id,
                device_name,
                comparison_sources,
                max_file_size,
                strict=True,
                nested_roots=nested_roots,
            )
            if remote_manifest is not None:
                exclude_map, skip_prefixes = core._build_exclude_map(config, comparison_sources)
                comparison_prior = core._filter_excluded_paths(
                    remote_manifest,
                    exclude_map,
                    skip_prefixes,
                    core._build_include_map(config, comparison_sources),
                )
                core._freeze_nested_checkout_entries(local_manifest, comparison_prior, nested_roots)
                comparison_prior = core._filter_symlinked_paths(
                    comparison_prior,
                    comparison_sources,
                    strict=True,
                    exempt_roots=nested_roots,
                )
                still_present: set[tuple[str, str]] = set()
                core._prove_omitted_paths_absent(
                    local_manifest,
                    comparison_prior,
                    comparison_sources,
                    max_file_size=max_file_size,
                    still_present=still_present,
                )
                # Track 91A: files a gone or new scan root hides are not deletions.
                mint_ledger, _ = core.load_ledger(())
                mint_guard = core.compute_mint_guard(
                    local_manifest,
                    comparison_prior,
                    comparison_sources,
                    mint_ledger,
                    still_present=still_present,
                )
                core.stamp_left_out(mint_guard, remote_manifest)
                left_out_clause = mint_guard.clause()
        except SnapshotError as exc:
            comparison_complete = False
            blocking = (
                "Source inspection incomplete. Restore readable, stable files, then run: mm status"
            )
            console.print(safe_str(safety.safe_terminal_str(str(exc))))
            local_manifest = {"sources": {}}
        remote_sources = remote_manifest.get("sources", {}) if remote_manifest else {}
        # Selection retirement is metadata too; disabled sources are deliberate.
        source_diffs = list(
            core.iter_content_diffs(local_manifest, remote_sources, source_filter=source)
        )
        pending_counts = core.content_counts(local_manifest, remote_sources, source_filter=source)
        metadata_pending = comparison_complete and (
            core._has_mtime_only_changes_vs_remote(
                local_manifest, remote_sources, source_filter=source
            )
            or (
                source is None
                and fetch.is_ok
                and set(remote_sources) != set(local_manifest["sources"])
            )
        )
        # Track 90A: the first push in the shared project-name form, or a rename,
        # is owed. Not under `--source other`, where it does not apply.
        claude_selected = any(src["name"] == "claude" for src in sources_configs)
        names_ready = names_held = False
        if comparison_complete and claude_selected and (source is None or source == "claude"):
            form = core.project_form_state(local_manifest, remote_manifest)
            names_held = form.switch_held
            names_ready = not names_held and bool(
                form.due or core.names_only_change(local_manifest, remote_sources)
            )
        try:
            n_conflicts = sum(
                source is None or hit[0] == source
                for hit in resolveflow._find_conflict_files(config)
            )
        except OSError:
            n_conflicts = 0
            blocking = "Conflict inspection incomplete. Restore read access, then run: mm status"
        auto_evidence = _read_autorun_evidence()
        attended_evidence = attemptlog.read_runs()
        projection = attemptlog.project_runs(auto_evidence, attended_evidence)
        if fetch.status == "corrupt":
            blocking = (
                core._newer_format_message(fetch.newer_version, "manifest")
                if fetch.newer_version is not None
                else "Own manifest is corrupt or incomplete. Run: mm push"
            )
        elif fetch.is_ok and fetch.conflict_copies_only:
            blocking = "Own manifest is corrupt or incomplete. Run: mm push"
        from mind_meld import host_usage as _host_usage

        publication = _host_publication(
            config, sources_configs, capture_rows, selected=source_resolution.selected
        )
        reader_states = {
            name: host_registry.reader_usage_diag(name) for name in publication["readers"]
        }
        for name, state in reader_states.items():
            if name in {"muse", "agy", "grok"} and state.get("last_reason") == "deadline":
                # Only deadline progress uses on-disk counts. Healthy status
                # stays cache-only instead of walking the host's full history.
                reader_states[name] = host_registry.reader_usage_diag(name, count_logs=True)
        # One cached read per command, never a refresh: the verdict, the blocker
        # lines and the Seam 3 display below all follow the same view.
        upgrade_result = upgrade.cached_upgrade_view(config)
        no_release = _no_release_readers(publication, reader_states, upgrade_result)
        capture_action = None
        if publication["readers"] and publication["readiness"] in ("disabled", "unavailable"):
            capture_action = "Usage capture unavailable. " + _usage_capture_remedy(
                publication, reader_states, upgrade_result
            )
            if publication["readiness"] == "unavailable":
                capture_action += " Run: mm diag"
        elif _usage_capture_needs_upgrade(publication, reader_states, upgrade_result):
            capture_action = "Usage capture needs a newer mm. Run: mm update"
        elif no_release:
            capture_action = "Usage not captured. " + _usage_capture_remedy(
                publication, reader_states, upgrade_result
            )
        elif publication["readiness"] == "ready" and any(
            state.get("last_reason") not in (None, "locked") for state in reader_states.values()
        ):
            capture_action = "Usage capture needs attention. Run: mm diag"

        devices = core._list_devices_warn(backend)
        peer_fetches: dict[str, Any] = {}
        peer_manifests: dict[str, dict | None] = {}
        for peer in devices:
            pid = peer.get("device_id")
            if not isinstance(pid, str) or not pid or pid == device_id:
                continue
            try:
                pfetch = core._fetch_remote_manifest(backend, pid, passphrase, memory_kb)
            except (OSError, MindMeldError) as exc:
                peer_fetches[pid] = exc
            else:
                peer_fetches[pid] = pfetch
                if pfetch.is_ok:
                    peer_manifests[pid] = pfetch.manifest
        try:
            with statedb.connect(read_only=True) as conn:
                cursors: dict[str, tuple[int | None, int | None, int]] | BaseException = (
                    statedb.read_cursors(conn)
                )
        except (statedb.StateDBError, OSError) as exc:
            cursors = exc
        storage_root = _backend_storage_path(backend, config)
        syncthing_state = syncthing.read_state(storage_root, stignore_line=initflow.STIGNORE_LINE)
        peer_states = _peer_states(
            devices,
            device_id,
            peer_fetches,
            cursors,
            syncthing_state,
            datetime.now(timezone.utc),
        )
        peers_overdue = _overdue_peers_line(peer_states)

        verdict = _status_verdict(
            source=source,
            blocking=blocking,
            conflicts=n_conflicts,
            projection=projection,
            counts=pending_counts,
            metadata=metadata_pending,
            first_push=fetch.status == "missing" and not fetch.vanished,
            capture_action=capture_action,
            manifest_arriving=fetch.status == "missing" and fetch.vanished,
            names_ready=names_ready,
            names_held=names_held,
            left_out=left_out_clause,
            peers_overdue=peers_overdue,
        )
        report["verdict"] = verdict
        if peers_overdue and peers_overdue not in verdict:
            console.print(safe_str(peers_overdue), soft_wrap=True)

        if claude_selected and (source is None or source == "claude"):
            for line in _project_name_lines(
                backend, devices, device_id, passphrase, memory_kb, manifests=peer_manifests
            ):
                console.print(line, soft_wrap=True)

        console.print("\n[bold]Mind Meld Status[/bold]")
        console.print(f"  Device: {safe_str(device_name)} ({safe_str(device_id)})")
        for st_line in _syncthing_status_lines(syncthing_state, storage_root):
            console.print(st_line, soft_wrap=True)
        nested_count = sum(
            len(roots) for name, roots in nested_roots.items() if source is None or name == source
        )
        if nested_count:
            console.print(f"  Skipped nested repositories: {nested_count}. Details: mm diag")

        # Surface the last autopull/autopush breadcrumb so a wedged sync
        # (silent lock contention, missing passphrase, bad config) is visible.
        crumbs = auto_evidence["runs"]
        for verb, state in projection.items():
            if state["kind"] == "attended" and state["selected"]:
                run = state["selected"]
                scope_note = f"; limited coverage {run['scope']}" if run["scope"] else ""
                console.print(
                    safe_str(
                        safety.safe_terminal_str(
                            f"  Last attended-{verb}: {run['completed_at']} "
                            f"({run['outcome']}: {run['cause']}{scope_note})"
                        )
                    )
                )
                count_labels = {
                    "held": "files not yet synced",
                    "total_failed": "files failed to apply",
                    "corrupt_peer_count": "corrupt peers",
                    "total_skipped_unknown_source": "unknown source pairs",
                    "durability_fsync_failures": "parent flush failures",
                    "maintenance_failures": "follow-up failures",
                }
                summary = [
                    f"{run['counts'][key]} {label}"
                    for key, label in count_labels.items()
                    if run["counts"].get(key)
                ]
                if summary:
                    console.print("    " + "; ".join(summary) + ". Details: mm diag")
                for detail in run["degradations"]:
                    console.print(safe_str(f"    {detail}"))
                if run["omitted_details"]:
                    console.print(
                        f"    {run['omitted_details']} diagnostic detail(s) not retained."
                    )
        discovery_nag = False
        for verb in ("pull", "push"):
            crumb = crumbs.get(verb)
            if projection[verb]["kind"] != "auto" or not crumb:
                continue
            ts = crumb.get("timestamp", "?")
            outcome = crumb.get("outcome", "?")
            # safe_str the peer-reachable fields before they hit a Rich
            # console, which interprets markup and passes escapes through.
            # `detail` is fed raw `str(e)` by the `failed` / `config-error` /
            # `crypto-error` breadcrumb writers, and those exceptions can
            # carry peer-derived text (device names, source names, rel_paths
            # from a peer manifest). This is the render site that MATTERS:
            # `mm status` is the command the v0.12.16 degradation signal is
            # designed to reach, and it runs far more often than `mm diag`.
            detail = crumb.get("detail")
            outcome_str = (
                f"{safe_str(safety.safe_terminal_str(outcome))}: "
                f"{safe_str(safety.safe_terminal_str(detail))}"
                if detail
                else safe_str(safety.safe_terminal_str(outcome))
            )
            # Staleness gate. `_write_autorun_breadcrumb` is called from INSIDE
            # the command, so a failure that happens before typer's runner --
            # an ImportError at module scope being the obvious one -- writes no
            # breadcrumb at all, and this line then reports the last SUCCESS
            # forever while sync is wedged. That is the one degradation the
            # v0.8.1 `no-sources` and v0.12.16 `degraded` breadcrumbs cannot
            # cover, because both are written by code that never ran.
            console.print(
                f"  Last auto-{safe_str(safety.safe_terminal_str(verb))}: "
                f"{safe_str(safety.safe_terminal_str(ts))} ({outcome_str})"
                f"{_breadcrumb_staleness_suffix(ts)}"
            )
            if outcome in ("keyring-locked", "keyring-no-collection"):
                console.print(
                    f"  [yellow]The last auto-{safe_str(safety.safe_terminal_str(verb))} "
                    "was skipped:[/yellow] "
                    f"keyring locked or unavailable; see {KEYRING_UNAVAILABLE_URL}"
                )
            if (
                verb == "pull"
                and outcome == "degraded"
                and isinstance(detail, str)
                and "file(s) failed" in detail
            ):
                console.print(
                    "  [yellow]Files failed to apply on the last auto-pull:[/yellow] "
                    "run [bold]mm pull[/bold] to see the warnings and retry"
                )
            if (
                verb == "push"
                and outcome == "degraded"
                and isinstance(detail, str)
                and "git repository discovery" in detail
            ):
                discovery_nag = True
        retro_nag = _print_retro_capture_status(sources_configs, device_id, scan=capture_rows)
        if discovery_nag and not retro_nag:
            console.print(
                "  [yellow]Git repository discovery incomplete:[/yellow] run [bold]mm diag[/bold]"
            )
        if fetch.status == "missing" and fetch.vanished:
            console.print(
                "  [dim]Remote manifest: still arriving from sync; check again shortly.[/dim]"
            )
        elif fetch.status == "missing":
            console.print("  [dim]Remote manifest: not yet pushed from this device.[/dim]")
        elif fetch.newer_version is not None:
            console.print(
                "  [yellow]Remote manifest requires a newer mm:[/yellow] "
                + core._newer_format_message(fetch.newer_version, "manifest")
            )
            return
        elif fetch.status == "corrupt":
            console.print(
                "  [yellow]Remote manifest: CORRUPT[/yellow] — next 'mm push' "
                "will attempt recovery from sidecar or peers."
            )

        # Visible-failure contract: surface the auto-command migration
        # breadcrumb so users notice their config is missing recommended
        # excludes (otherwise autopull/autopush silently keep producing
        # conflict copies for repo-mode.json / land-deploy-confirmed every
        # pull, with no signal that `mm migrate-config` would fix it).
        missing_excludes = core._config_missing_recommended_excludes(config)
        if missing_excludes:
            console.print(
                f"  [yellow]Config missing recommended excludes for source(s):[/yellow] "
                f"{', '.join(safe_str(name) for name in missing_excludes)} — "
                "run [bold]mm migrate-config[/bold] to add."
            )

        if n_conflicts:
            console.print(
                f"  [yellow]Unresolved conflicts: {n_conflicts}[/yellow] — "
                "run [bold]mm conflicts[/bold] to review, "
                "[bold]mm resolve[/bold] to pick a winner."
            )

        # Allowlist the BROKEN states, never a denylist of healthy ones. The full
        # argument lives beside the constant in `skill_link.BROKEN_SKILL_STATUSES`
        # -- do not restate the working-as-intended list here, it drifted once
        # already (this copy omitted `absent` and then `removed-by-user`).
        # Same shape as the Grok refusal that
        # pinned the breadcrumb at `degraded` and destroyed it as a signal. A
        # denylist also defaults every FUTURE status to "broken".
        skill_may_create = skill_link.consented_agent_keys(config, sources_configs)
        broken_skills = [
            row
            for row in skill_link.diagnose_skill_links(may_create=skill_may_create)
            if row.get("status") in skill_link.BROKEN_SKILL_STATUSES
        ]
        if broken_skills:
            rendered = _render_broken_skill_status(broken_skills[0])
            if "restart the agent so it reloads SKILL.md" not in rendered:
                rendered = f"{rendered}, then restart the agent so it reloads SKILL.md"
            console.print(f"  [yellow]Skill links broken:[/yellow] {rendered}")

        # Seam 3 — status inspects cached upgrade knowledge without refreshing it
        # (the view was read once, before the usage-capture verdict).
        if upgrade_result.state == "upgrade-available" and upgrade_result.latest:
            console.print(
                f"  [yellow]Upgrade available:[/yellow] "
                f"{safe_str(upgrade_result.local)} → {safe_str(upgrade_result.latest)} "
                f"(run [bold]{safe_str(upgrade_result.install_cmd)}[/bold])"
            )
            if upgrade_result.install_attempt == "failed":
                console.print(
                    "  [yellow]Automatic update did not complete:[/yellow] "
                    "run [bold]mm update[/bold] to see why."
                )
        elif upgrade_result.state == "unknown":
            reason = (
                "contended"
                if upgrade_result.cache_state == "lock_failed"
                else upgrade_result.cache_state
            )
            console.print(f"  Upgrade check: unknown ({safe_str(reason)} cache).")
        if upgrade_result.stale and upgrade_result.checked_at is not None:
            age = datetime.now(timezone.utc) - upgrade_result.checked_at
            console.print(f"  [dim]Upgrade cache: checked {age.days}d ago (stale).[/dim]")

        # Per-machine source-toggle visibility (v0.10.0). Two breadcrumbs:
        #   1. Disabled list: surfaces intentional state so future-you doesn't
        #      forget gstack is off and re-debug "why isn't this syncing".
        #   2. New-source hint: when DEFAULT_SOURCES grows (codex et al.) the
        #      user sees a one-shot suggestion to opt in. seen_sources tracks
        #      acknowledgments via enable/disable/reconfigure; the lazy-init
        #      invariant in seen_sources.read() ensures upgraders don't see
        #      spurious hints for already-shipped claude/gstack.
        disabled_list = list(config.get("sync", {}).get("disabled_sources", []) or [])
        if disabled_list:
            resolvable = {s["name"] for s in core._resolve_all_configured_sources(config)}
            shown_disabled = [name for name in disabled_list if name in resolvable]
            if shown_disabled:
                console.print(
                    f"  [yellow]Disabled sources (this device):[/yellow] "
                    f"{', '.join(safe_str(name) for name in sorted(shown_disabled))} — "
                    "run [bold]mm enable-source <name>[/bold] to re-enable."
                )

        explicit_names = [s["name"] for s in config.get("sync", {}).get("sources", []) or []]
        currently_resolved = [s["name"] for s in sources_configs]
        seen = seen_sources.read(initial=currently_resolved)
        default_names = [s["name"] for s in DEFAULT_SOURCES]
        new_sources = seen_sources.compute_new_sources(
            seen=seen,
            default_names=default_names,
            disabled=disabled_list,
            explicit_names=explicit_names,
        )
        for name in new_sources:
            console.print(
                f"  [cyan]New source available:[/cyan] {safe_str(name)} — "
                f"run [bold]mm enable-source {safe_str(name)}[/bold] to sync, "
                f"or [bold]mm disable-source {safe_str(name)}[/bold] to dismiss."
            )

        capture_remedy = _usage_capture_remedy(publication, reader_states, upgrade_result)
        upgrade_required = _usage_capture_needs_upgrade(publication, reader_states, upgrade_result)
        _print_host_publication(
            publication,
            reader_states=reader_states,
            hide_superseded=True,
            upgrade_view=upgrade_result,
        )

        # Enabling the Codex source is consent. A standing blocker takes priority
        # over inventory-based remedies; healthy Codex capture stays quiet here.
        if any(s.get("name") == "codex" for s in sources_configs):
            codex_diag = reader_states["codex"]
            if codex_diag.get("last_reason"):
                console.print(
                    "  Codex usage capture: "
                    + _host_usage_blocker(
                        "codex",
                        codex_diag,
                        readiness=_reader_capture_readiness(publication, "codex"),
                        upgrade_required=upgrade_required,
                        no_release_reads=no_release,
                    )
                )
            elif codex_diag.get("files_pre_track"):
                console.print(
                    f"  Codex usage capture: rebuilding — {codex_diag['files_pre_track']} rollouts "
                    f"awaiting re-walk; {safe_str(capture_remedy)}"
                )
            elif codex_diag.get("state") == "migrating":
                console.print(
                    f"  Codex usage capture: warming — {codex_diag.get('pending') or 0} rollouts "
                    f"not yet scanned; {safe_str(capture_remedy)}"
                )

        muse_on = any(s.get("name") == "muse" for s in sources_configs)
        # The presence probe only feeds the opt-in nudge: skip it once Muse is
        # enabled (its reader state answers) or explicitly disabled (no nudge).
        muse_disabled = "muse" in (config.get("sync", {}).get("disabled_sources") or [])
        muse_present = False
        if not muse_on and not muse_disabled:
            try:
                muse_present = _host_usage.muse_sessions_root().exists()
            except OSError:
                muse_present = False
        if muse_on or muse_present:
            if muse_on:
                muse_diag = reader_states["muse"]
                if muse_diag.get("last_reason"):
                    console.print(
                        "  Muse usage capture: enabled — "
                        + _host_usage_blocker(
                            "muse",
                            muse_diag,
                            readiness=_reader_capture_readiness(publication, "muse"),
                            upgrade_required=upgrade_required,
                            no_release_reads=no_release,
                        )
                    )
                elif muse_diag.get("complete_once") is True:
                    console.print(
                        "  Muse usage capture: enabled; a prior scan completed successfully"
                    )
                else:
                    console.print(
                        "  Muse usage capture: enabled, but no successful scan yet — "
                        + safe_str(capture_remedy)
                    )
            else:
                console.print(
                    "  Muse usage capture: disabled — "
                    "run [bold]mm enable-source muse[/bold] to sync Muse memory "
                    "and publish token totals (session files stay local)."
                )

        agy_on = "agy" in reader_states
        agy_disabled = "agy" in (config.get("sync", {}).get("disabled_sources") or [])
        agy_present = False
        if not agy_on and not agy_disabled:
            try:
                agy_present = _host_usage.agy_sessions_root().exists()
            except OSError:
                agy_present = False
        if agy_on or agy_present:
            if agy_on:
                agy_diag = reader_states["agy"]
                if agy_diag.get("last_reason"):
                    console.print(
                        "  Antigravity usage capture: enabled — "
                        + _host_usage_blocker(
                            "agy",
                            agy_diag,
                            readiness=_reader_capture_readiness(publication, "agy"),
                            upgrade_required=upgrade_required,
                            no_release_reads=no_release,
                        )
                    )
                elif agy_diag.get("complete_once") is True:
                    console.print(
                        "  Antigravity usage capture: enabled; a prior scan completed successfully"
                    )
                else:
                    console.print(
                        "  Antigravity usage capture: enabled, but no successful scan yet — "
                        + safe_str(capture_remedy)
                    )
            else:
                console.print(
                    "  Antigravity usage capture: disabled — "
                    "run [bold]mm enable-source agy[/bold] to sync Antigravity configuration "
                    "and publish token totals (conversation DBs stay local)."
                )

        if "grok" in reader_states:
            # Present only when Grok usage capture is consented: no presence probe and no
            # nudge when it is not, so an unconsented ``~/.grok`` is never opened here.
            grok_diag = reader_states["grok"]
            if grok_diag.get("last_reason"):
                console.print(
                    "  Grok Build usage capture: enabled — "
                    + _host_usage_blocker(
                        "grok",
                        grok_diag,
                        readiness=_reader_capture_readiness(publication, "grok"),
                        upgrade_required=upgrade_required,
                        no_release_reads=no_release,
                    )
                )
            elif grok_diag.get("complete_once") is True:
                console.print(
                    "  Grok Build usage capture: enabled; a prior scan completed successfully"
                )
            else:
                console.print(
                    "  Grok Build usage capture: enabled, but no successful scan yet — "
                    + safe_str(capture_remedy)
                )

        if "cursor" in reader_states:
            cursor_diag = reader_states["cursor"]
            if cursor_diag.get("last_reason"):
                narrative = _host_usage_blocker(
                    "cursor",
                    cursor_diag,
                    readiness=_reader_capture_readiness(publication, "cursor"),
                    upgrade_required=upgrade_required,
                    no_release_reads=no_release,
                )
                detail_clause = _cursor_store_detail_clause(cursor_diag.get("last_reason_detail"))
                if detail_clause:
                    narrative += "; " + safe_str(detail_clause)
            elif cursor_diag.get("complete_once") is True:
                narrative = "enabled; a prior scan completed successfully"
            else:
                narrative = "enabled, but no successful scan yet — " + safe_str(capture_remedy)
            hook_state = cursor_diag.get("hook_state")
            if hook_state == "missing":
                narrative += (
                    "; standalone interactive sessions are not captured until "
                    "mm enable-source cursor installs the completion hook"
                )
            elif hook_state not in (None, "installed"):
                narrative += f"; the completion hook file is {safe_str(hook_state)} (see mm diag)"
            narrative += (
                "; Cursor runs through ACP front-ends such as Paseo are not counted "
                "(they report no usage)"
            )
            unread = cursor_diag.get("unread_sqlite_stores")
            if isinstance(unread, int) and unread > 0:
                narrative += (
                    f"; {unread} Conductor workspace store(s) not yet read; "
                    "mm push attempts to read them"
                )
            unsupported = cursor_diag.get("unsupported_sqlite_stores")
            if isinstance(unsupported, dict) and unsupported.get("count"):
                narrative += "; " + safe_str(_cursor_unsupported_clause(unsupported))
            console.print(
                f"  {host_registry.HOST_READER_DIAGS['cursor'].label} usage capture: " + narrative
            )

        # Per-source breakdown
        total_local = 0
        total_remote = 0
        total_new = 0
        total_modified = 0
        total_deleted = 0

        for src_name, src_data, remote_src, diff in source_diffs:
            local_files = src_data["files"]
            remote_files = remote_src.get("files", {})

            local_count = len(local_files)
            remote_count = len(remote_files)
            total_local += local_count
            total_remote += remote_count
            total_new += len(diff.new)
            total_modified += len(diff.modified)
            total_deleted += len(diff.deleted)

            console.print(f"\n  [bold]Source '{safe_str(src_name)}':[/bold]")
            console.print(f"    Local files: {local_count}")
            console.print(f"    Remote files: {remote_count}")

            if diff.has_changes:
                console.print("    [yellow]Pending push:[/yellow]")
                if diff.new:
                    console.print(f"      + {len(diff.new)} new")
                if diff.modified:
                    console.print(f"      ~ {len(diff.modified)} modified")
                if diff.deleted:
                    console.print(f"      - {len(diff.deleted)} deleted")
            else:
                console.print("    [green]In sync.[/green]")

        if not source:
            console.print(f"\n  Total local: {total_local}, Total remote: {total_remote}")

        if not comparison_complete or blocking:
            console.print("\n  Comparison incomplete; see the verdict above.")
        elif total_new or total_modified or total_deleted:
            console.print("\n  [yellow]Overall pending push:[/yellow]")
            console.print(
                f"    + {total_new} new, ~ {total_modified} modified, - {total_deleted} deleted"
            )
        elif names_ready:
            console.print(
                "\n  [yellow]Project memory ready to publish under shared names[/yellow] "
                "(run `mm push`)."
            )
        elif metadata_pending:
            # Symmetric with _push_core's mtime-only republish gate (v0.12.6).
            # Without this branch, status would print "All sources in sync"
            # after `mm resolve (l)ocal` even though a push is needed to
            # propagate the bumped mtime to peers. source_filter mirrors the
            # iter_source_diffs filter above so `mm status --source X` doesn't
            # surface metadata-pending hints from sources Y, Z.
            console.print(
                "\n  [yellow]Metadata-only changes pending[/yellow] "
                "(run `mm push` to publish updated mtimes)."
            )
        elif not source and not names_held:
            console.print("\n  Local content matches this device's last push.")
        if names_held:
            console.print(
                "\n  Project memory not published until Claude's projects folder can be listed."
            )

        if peer_states:
            console.print("\n  Other devices:")
            for p in peer_states:
                label = (
                    f"{safety.safe_terminal_str(p.device_name)} "
                    f"({safety.safe_terminal_str(p.device_id)})"
                )
                console.print(
                    safe_str(f"    {label}: {_format_peer_state(p)}"),
                    soft_wrap=True,
                )


_DISCOVERY_REJECT_REASONS = ("gone", "not-a-repo", "unreadable")


_DIAG_MODELS_SHOWN = 6
"""How many model ids the plain-text ``mm diag`` names inline.

The producer already caps the list at ``host_usage._DIAG_MODEL_CAP`` (32);
this second, smaller bound is a readability one — the block is optimized for
paste into a support chat, and 32 ids on one line is not that. The COUNT is
always exact, so a truncated list never reads as the whole set.
"""


def _reader_capture_readiness(
    publication: dict, reader: str
) -> _config_module.UsageCaptureReadiness:
    readiness = publication["readiness"]
    if readiness == "ready" and reader not in publication["readers"]:
        return "no-reader"
    return readiness


_CURSOR_DETAIL_NO_CLAUSE = frozenset({"hot_journal", "identity_changed"})
"""Transient causes: the next read retries, so there is nothing for the user to do."""


_CURSOR_ROW_CAUSES = frozenset(
    {
        "bad_json",
        "oversize_or_type",
        "bad_timestamp",
        "bad_utf8",
        "bad_counters",
        "bad_model",
        "corrupt_database",
    }
)


_CURSOR_DISABLE = "`mm disable-source cursor`"


def _sqlite_version() -> str:
    try:
        import sqlite3

        return sqlite3.sqlite_version
    except ImportError:
        return "unavailable"


def _cursor_unsupported_clause(unsupported: dict) -> str:
    """Store-scoped drift: these stores contribute nothing until a newer mm reads them."""
    count = unsupported["count"]
    causes = [str(cause) for cause in unsupported.get("causes") or []]
    clause = (
        f"{count} Conductor workspace store(s) use a layout this mm cannot read "
        f"({', '.join(causes)}); their runs are not counted. Run `mm update`; "
        f"if you are current, see {CURSOR_SQLITE_URL}"
    )
    if "schema_unreadable" in causes:
        clause += f" (this mm's Python links SQLite {_sqlite_version()})"
    return clause


def _cursor_store_detail_clause(detail: dict | None) -> str | None:
    """Closed-catalog remedy for the persisted Cursor SQLite cause, or None.

    Rendered from ``last_reason_detail`` only; the store id is the one persisted
    field behind the path. Never advises deleting retained history or the spool."""
    if not isinstance(detail, dict):
        return None
    cause = detail.get("cause")
    if cause in _CURSOR_DETAIL_NO_CLAUSE or not isinstance(cause, str):
        return None
    store = detail.get("store")
    path = (
        core._home_relative_path(_host_usage_module().CURSOR_STORE_PATH / store)
        if isinstance(store, str)
        else "(unknown store)"
    )
    if cause in _CURSOR_ROW_CAUSES:
        clause = (
            f"Conductor workspace store {path} holds a row mm cannot read ({cause}); mm "
            "withholds Cursor usage on this Mac until it reads cleanly. If it persists, "
            f"{_CURSOR_DISABLE} (or `[retro] cursor_host_usage = false`) stops Cursor usage "
            f"reading. See {CURSOR_SQLITE_URL}"
        )
        if cause == "corrupt_database":
            clause += f" (this mm's Python links SQLite {_sqlite_version()})"
        return clause
    if cause in {"duplicate_request", "conflict"}:
        return (
            f"Conductor run records conflict ({cause}); mm withholds "
            f"Cursor usage until they agree. If it persists, {_CURSOR_DISABLE} stops Cursor "
            "usage reading"
        )
    if cause == "non_regular_path":
        shown = detail.get("file") or "index.db"
        return (
            f"{shown} in Conductor workspace store {path} is a symlink or special file; "
            f"replace it with Conductor's own file, or {_CURSOR_DISABLE}"
        )
    if cause == "cannot_open":
        code = detail.get("code") or "unknown SQLite error"
        return (
            f"mm cannot open Conductor workspace store {path} ({code}); restore read access "
            f"to Conductor's cursor-sdk-store, or {_CURSOR_DISABLE}"
        )
    if cause == "sqlite_unavailable":
        return SQLITE_UNAVAILABLE_REMEDY
    if cause == "sqlite_pragma":
        return (
            f"mm's Python links SQLite {_sqlite_version()}, which lacks a required "
            "capability; reinstall mm as described in the README Install section"
        )
    return None


def _host_usage_module():
    from mind_meld import host_usage

    return host_usage


def _host_usage_blocker(
    reader: str,
    state: dict,
    *,
    readiness: _config_module.UsageCaptureReadiness,
    upgrade_required: bool = False,
    no_release_reads: Sequence[str] = (),
    in_diag: bool = False,
) -> str:
    """Render cache-validated blocker metadata for both diagnostic surfaces."""
    reason = state.get("last_reason")
    if reason is None:
        return "none"
    cached = state.get("runs_cached") if reader == "cursor" else state.get("files_cached")
    on_disk = None if reader == "cursor" else state.get("files_on_disk")
    evidence = events_tail.HostReadEvidence(
        last_deadline_allotted_ms=state.get("last_deadline_allotted_ms"),
        last_complete_ms=state.get("last_complete_ms"),
        last_complete_at=state.get("last_complete_at"),
        files_cached=cached,
        files_on_disk=on_disk,
    )
    phrase = events_tail._host_skip_phrase(
        reader,
        reason,
        readiness=readiness,
        evidence=evidence,
        in_diag=in_diag,
        upgrade_required=upgrade_required,
        no_release_reads=no_release_reads,
    )
    since = state.get("last_reason_since")
    if since is not None:
        try:
            day = datetime.fromisoformat(since).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            day = None
        if day is not None:
            phrase += f" (first observed {day} UTC)"
    return safe_str(phrase)


def _host_read_budgets(config: dict | None) -> dict:
    from mind_meld import host_usage

    budgets = {
        "autopush_ms": None,
        "autopush_source": None,
        "interactive_ms": None,
        "interactive_source": None,
        "capture_ms": None,
        "capture_source": None,
        "warm_ms": round(host_usage.DEFAULT_READ_BUDGET_S * 1000),
    }
    if config is not None:
        # Display-only: the resolver stashes the capture total for the next
        # attended snapshot; a diag read must not plant or clear it.
        with events_tail.preserve_pending_capture_budget():
            for name in ("autopush", "interactive"):
                budgets[f"{name}_ms"], budgets[f"{name}_source"] = (
                    events_tail.resolve_host_read_budget(config, interactive=name == "interactive")
                )
            budgets["capture_ms"], budgets["capture_source"] = events_tail.resolve_capture_budget(
                config
            )
    return budgets


def _host_complete_read_line(state: dict) -> str:
    elapsed = state.get("last_complete_ms")
    at = state.get("last_complete_at")
    if elapsed is None and at is None:
        return "unknown"
    return (
        f"scan {'unknown' if elapsed is None else elapsed} ms (excludes cache write), "
        f"{events_tail.host_read_age(at)} ({at or 'unknown'})"
    )


def _host_read_sweep_line(state: dict) -> str:
    budget = state["host_read_budgets"]["autopush_ms"]
    if state["config"]["state"] != "ok" or budget is None:
        return "unknown (config unavailable)"
    # Publication's reader keys are the current consented/resolved reader set
    # from _default_host_readers, even when no row exists. Coverage VALUES do
    # not participate: this estimate is never evidence of publication.
    readers = state["host_publication"]["readers"]
    if not readers:
        return "unknown (no reader enabled)"
    elapsed = [state["host_usage"].get(name, {}).get("last_complete_ms") for name in readers]
    if any(value is None for value in elapsed):
        return "unknown (a reader has no complete read)"
    total = sum(elapsed)
    remaining = budget - total
    margin = f"{remaining} ms spare" if remaining >= 0 else f"over by {-remaining} ms"
    return (
        f"{total} ms of the {budget} ms autopush budget, {margin} "
        "(excludes cache writes and the later-reader grace; not a publication check)"
    )


def _diag_models_line(state: dict) -> str:
    """One line of locally-cached model ids for a host-usage reader.

    Cache-only, like everything else in this block: these ids come from the
    reader's own private cache, never from a peer manifest. They still go
    through ``safe_str`` because a host log wrote them and Rich interprets
    markup in an f-string — the same reasoning as the breadcrumb ``detail``
    render above.

    ``safe_str`` alone is NOT enough here. It strips terminal escapes and Rich
    markup but leaves newlines, so a model id of ``"gpt-5\ngrok cache: ok"``
    forges an extra field into a block a user pastes into a support chat.
    Bucket to the same conservative whitelist ``aggregator._safe_short`` uses
    for the identical class of string; the two surfaces should not disagree
    about what a model id may contain.

    ``0`` is a real answer, not a gap: a cold or mid-rebuild cache has
    interned no ids yet, which is exactly what ``codex reader state`` two
    lines up already says.
    """
    count = state.get("model_count")
    count = count if isinstance(count, int) and not isinstance(count, bool) else 0
    raw = state.get("models")
    names = (
        [re.sub(r"[^A-Za-z0-9._\-() ]", "_", safe_str(str(m)))[:60] for m in raw]
        if isinstance(raw, list)
        else []
    )
    if not count or not names:
        return str(count)
    shown = names[:_DIAG_MODELS_SHOWN]
    hidden = count - len(shown)
    listed = ", ".join(shown) + (f", +{hidden} more" if hidden > 0 else "")
    return f"{count} ({listed})"


_DIAG_CAPTURE_CLAMP = 128
"""Bound on peer-controlled strings in the recorded-capture diag block.

The mm-push row arrives via the pull apply path and ``merge.merge_jsonl``,
so ``ts`` / ``mm_version`` / ``since`` / discovery labels are peer text.
The invariant requires bounding, not merely sanitizing; 128 matches
``aggregator._safe_short``."""


def _read_capture_rows(sources: list[dict], device_id: str | None) -> events.EventScan:
    """One day-file pass for both diagnostic blocks, using the fleet acceptor."""
    from mind_meld.skills.retro_fleet import aggregator

    src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if not device_id or src is None:
        return events.EventScan(errors=["config invalid or mm-events unavailable"])
    now = datetime.now(timezone.utc)
    return events.latest_event_rows(
        Path(src["path"]).expanduser() / "events",
        device_id,
        {"mm-push", "host-usage-snapshot"},
        now=now,
        selectors={
            "host-usage-snapshot": lambda row: aggregator.local_host_capture_candidate(
                row, until=now
            )
        },
        day_margins={"host-usage-snapshot": aggregator._HOST_FUTURE_SKEW},
    )


def _host_publication(
    config: dict, sources: list[dict], scan: events.EventScan, *, selected: list[dict] | None
) -> dict:
    readers = [
        name
        for name, _ in events_tail._default_host_readers(
            sources,
            cursor_consented=_config_module.cursor_host_usage_enabled(config),
            grok_consented=_config_module.grok_usage_consented(config),
        )
    ]
    try:
        manifest = sidecar.read(config["device"]["id"])
    except (OSError, MindMeldError, KeyError, RecursionError):
        manifest = None
    projected = events.project_host_publication(scan, readers, manifest)
    projected.update(attemptlog.project(readers, projected.get("ts")))
    projected["readiness"] = _config_module.usage_capture_readiness(
        selected, sources, reader_consented=bool(readers)
    )
    if scan.errors and projected["state"] == "unknown":
        # Local paths must remain recognizable even under a long home prefix.
        projected["error"] = safe_str(core._home_relative_path(Path(scan.errors[0])))
    return projected


def _print_host_publication(
    state: dict,
    *,
    reader_states: dict[str, dict],
    hide_superseded: bool = False,
    upgrade_view: upgrade.UpgradeCheckResult | None = None,
) -> None:
    """Keep recorded coverage, publication evidence and attempts distinct."""
    if state["state"] == "absent":
        recorded = f"no capture in the last {events.CURSOR_SCAN_DAYS} days"
    elif state["state"] == "unknown":
        recorded = f"unknown ({state.get('error', 'events unreadable')})"
    else:
        recorded = f"{state['ts']} ({state['age_days']}d ago)"
    console.print(f"  Host usage last recorded capture: {safe_str(recorded)}")
    reason = f" ({state['publication_reason']})" if state.get("publication_reason") else ""
    console.print(
        f"    Publication: {safe_str(state['publication'] + reason)} (accepted manifest evidence)"
    )

    for reader, coverage in state["readers"].items():
        if reader not in events.HOST_USAGE_TOKEN_SOURCES:
            continue
        outcome = "empty" if reader in (state.get("empty_readers") or ()) else coverage
        descriptor = host_registry.HOST_READER_DIAGS.get(reader)
        label = descriptor.label if descriptor is not None else reader
        console.print(f"    {safe_str(label)}: {safe_str(events.host_reader_label(outcome))}")
    for line in (
        []
        if hide_superseded and state.get("latest_attempt_superseded")
        else attemptlog.render(
            state,
            age=events_tail.host_read_age(state.get("latest_attempt_at")),
            path=core._home_relative_path(attemptlog.record_path()),
            refresh_ready=state["readiness"] == "ready"
            and not _unsupported_readers(state, reader_states),
        )
    ):
        console.print(f"    {safe_str(line)}")
    if state.get("coverage_invalid"):
        console.print("    Coverage metadata is inconsistent; reader completeness is unknown.")
    if state["readiness"] != "ready" or (
        state["readers"]
        and (
            state["state"] != "recorded"
            or (state.get("age_days") or 0) > 0
            or state.get("coverage_invalid")
            or any(v != "contributed" for v in state["readers"].values())
        )
    ):
        remedy = _usage_capture_remedy(state, reader_states, upgrade_view)
        label = "Usage refresh" if state["readiness"] == "ready" else "Usage capture unavailable"
        console.print(f"    {label}: {safe_str(remedy)}")


def _unsupported_readers(publication: dict, reader_states: dict[str, dict]) -> tuple[str, ...]:
    """Consented readers whose standing blocker is a record format mm cannot read."""
    if publication["readiness"] != "ready":
        return ()
    return tuple(
        reader
        for reader in publication["readers"]
        if reader_states.get(reader, {}).get("last_reason") in events_tail._HOST_PERMANENT_REASONS
    )


def _no_release_readers(
    publication: dict,
    reader_states: dict[str, dict],
    upgrade_view: upgrade.UpgradeCheckResult | None,
) -> tuple[str, ...]:
    """Unsupported readers when this release is the newest the cached check knows.

    Only a ``current`` view counts. ``unknown`` (no usable cache), ``skip`` and a
    cached newer release keep ``mm update`` as the remedy; the view is never
    refreshed here.
    """
    if upgrade_view is None or upgrade_view.state != "current":
        return ()
    return _unsupported_readers(publication, reader_states)


def _usage_capture_needs_upgrade(
    publication: dict,
    reader_states: dict[str, dict],
    upgrade_view: upgrade.UpgradeCheckResult | None = None,
) -> bool:
    """True when an unsupported format is present and an update may still read it."""
    return bool(_unsupported_readers(publication, reader_states)) and not _no_release_readers(
        publication, reader_states, upgrade_view
    )


def _usage_capture_remedy(
    publication: dict,
    reader_states: dict[str, dict],
    upgrade_view: upgrade.UpgradeCheckResult | None = None,
) -> str:
    """Pair the shared prerequisite verdict with currently consented reader blockers."""
    no_release = _no_release_readers(publication, reader_states, upgrade_view)
    if no_release:
        return events_tail._no_release_reads_clause(no_release)
    if _unsupported_readers(publication, reader_states):
        return "Run mm update; a consented reader has an unsupported record format."
    return _config_module.usage_capture_remedy(publication["readiness"])


def _print_retro_capture_status(
    sources: list[dict], device_id: str, *, scan: events.EventScan | None = None
) -> bool:
    """Nag on an incomplete recorded capture. Returns True if a nag printed."""
    mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if mm_events_src is None:
        return False
    events_dir = Path(mm_events_src["path"]).expanduser() / "events"
    row = (
        scan.rows.get("mm-push")
        if scan is not None
        else events.latest_mm_push_row(events_dir, device_id)
    )
    projected = events.project_recorded_capture(row)
    if projected is None:
        return False
    aborts = projected.get("walk_budget_aborts") or 0
    errs = projected.get("walk_errors") or 0
    skipped = aborts + errs
    pending_since = datetime.now(timezone.utc) - timedelta(days=events.INITIAL_CURSOR_LOOKBACK_DAYS)
    if isinstance(projected.get("ts"), str):
        try:
            observed = datetime.fromisoformat(projected["ts"])
            if observed.tzinfo is not None:
                pending_since = observed
        except ValueError:
            pass
    pending = events.count_pending_repositories([], pending_since)
    cache = events.read_git_capture_cache()
    incomplete = (
        not projected.get("advances_cursor", True)
        or skipped > 0
        or pending > 0
        or bool(cache.get("pending_refresh_since"))
    )
    if not incomplete:
        return False
    cursor = (
        scan.cursor
        if scan is not None and scan.cursor is not None
        else events.resolve_push_cursor(events_dir, device_id)
    )
    since_s = projected.get("since") or projected.get("ts")
    since_day = since_s[:10] if isinstance(since_s, str) and len(since_s) >= 10 else "unknown"
    days = events.INITIAL_CURSOR_LOOKBACK_DAYS
    if isinstance(since_s, str):
        try:
            since_dt = datetime.fromisoformat(since_s)
            if since_dt.tzinfo is not None:
                delta_days = (datetime.now(timezone.utc) - since_dt).days
                days = max(1, min(events.RECAPTURE_WINDOW_MAX_DAYS, delta_days or 1))
        except (TypeError, ValueError):
            pass
    capture_since = cursor.since
    if isinstance(since_s, str):
        try:
            observed_since = datetime.fromisoformat(since_s)
            if observed_since.tzinfo is not None:
                capture_since = min(capture_since, observed_since)
        except ValueError:
            pass
    recovery_required = (
        cursor.floored_after_incomplete
        or datetime.now(timezone.utc) - events.effective_capture_since([], capture_since)
        > timedelta(days=events.CURSOR_SCAN_DAYS)
        or projected.get("discovery") == "empty"
        or (aborts == 0 and errs > 0)
        or cache.get("incomplete_streak", 0) >= 3
    )
    if not recovery_required:
        noun = "repository" if pending == 1 else "repositories"
        qualifier = "known " if pending == 0 else ""
        console.print(
            f"  [yellow]Retro capture:[/yellow] {pending} {qualifier}{noun} left to catch up."
        )
        if projected.get("discovery") == "partial":
            console.print("  Repository discovery is still catching up.")
        console.print("  Continue capture on this Mac: [bold]mm push[/bold]")
        console.print("  Details: [bold]mm diag[/bold]")
        return True
    if skipped == 1:
        skip_phrase = "1 repository was skipped"
    elif skipped > 1:
        skip_phrase = f"{skipped} repositories were skipped"
    elif projected.get("discovery") == "partial":
        skip_phrase = "repository discovery has not finished"
    else:
        skip_phrase = "repository set incomplete"
    console.print(
        f"  [yellow]Retro capture:[/yellow] incomplete since {safe_str(since_day)} — {skip_phrase}."
    )
    if projected.get("discovery") == "partial":
        console.print(
            f"  Finish repository discovery and recover this Mac's last {days} days: "
            f"[bold]mm recapture {days}d[/bold]"
        )
    else:
        console.print(f"  Recover this Mac's last {days} days: [bold]mm recapture {days}d[/bold]")
    console.print("  Check current repository failures: [bold]mm recapture --dry-run[/bold]")
    console.print("  Details: [bold]mm diag[/bold]")
    return True


def _clamp_peer_text(value: object) -> str:
    return safe_str(str(value))[:_DIAG_CAPTURE_CLAMP]


def _sanitize_recorded_capture(projected: dict) -> dict:
    out = dict(projected)
    for key in ("ts", "mm_version", "discovery", "since"):
        if out.get(key) is not None:
            out[key] = _clamp_peer_text(out[key])
    return out


def _fresh_discovery_label(diag: dict) -> str:
    status = diag.get("status")
    if status in ("exceeded", "error"):
        return "partial"
    if status == "empty":
        return "empty"
    if status == "no-prober":
        return "not-run"
    if status == "complete":
        return "complete"
    return "not-run"


def _collect_git_capture_diag(
    config: dict, device_id: str | None, discovery: dict, *, scan: events.EventScan | None = None
) -> dict:
    recorded = None
    if device_id:
        try:
            sources = get_sources(config) if config else []
            src = next((s for s in sources if s.get("name") == "mm-events"), None)
            if src is not None:
                events_dir = Path(src["path"]).expanduser() / "events"
                projected = events.project_recorded_capture(
                    scan.rows.get("mm-push")
                    if scan is not None
                    else events.latest_mm_push_row(events_dir, device_id)
                )
                if projected is not None:
                    recorded = _sanitize_recorded_capture(projected)
        except Exception:
            recorded = None
    since = datetime.now(timezone.utc) - timedelta(days=events.INITIAL_CURSOR_LOOKBACK_DAYS)
    if recorded and isinstance(recorded.get("ts"), str):
        try:
            observed = datetime.fromisoformat(recorded["ts"])
            if observed.tzinfo is not None:
                since = observed
        except ValueError:
            pass
    pending = events.count_pending_repositories(discovery.get("roots") or [], since)
    return {
        "recorded": recorded,
        "fresh": {
            "pending_repos": pending,
            "discovery": _fresh_discovery_label(discovery),
            "status": discovery.get("status"),
            "roots": len(discovery.get("roots") or []),
            "exceeded": bool(discovery.get("exceeded")),
            "error_count": len(discovery.get("errors") or []),
        },
    }


def _collect_discovery_diag(config: dict) -> dict:
    """Run git-root discovery at the AUTOPUSH budget and shape it for diag.

    Autopush is the only path a Claude Code hook fires and the only one that
    loses data; reporting complete at the interactive 100 ms budget while
    autopush is exceeded at 50 ms would point the verification surface at
    the wrong number.
    """
    budget_ms = events.ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS
    started = time.monotonic()
    try:
        result = events.discover_git_roots(
            config or {},
            deadline_monotonic=started + budget_ms / 1000.0,
        )
    except Exception as e:
        return {
            "budget_ms": budget_ms,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "status": "error",
            "error": f"{type(e).__name__}: {safe_str(e)}",
            "probers_ran": [],
            "roots": [],
            "attribution": [],
            "rejects": {"counts": {}, "sample": []},
            "errors": [],
            "exceeded": False,
        }
    elapsed_ms = round((time.monotonic() - started) * 1000, 3)
    if result.exceeded:
        status = "exceeded"
    elif not result.probers_ran:
        status = "no-prober"
    elif result.roots:
        status = "complete"
    else:
        status = "empty"
    counts = {reason: 0 for reason in _DISCOVERY_REJECT_REASONS}
    for reason, n in result.reject_counts:
        counts[reason] = n
    return {
        "budget_ms": budget_ms,
        "elapsed_ms": elapsed_ms,
        "status": status,
        "exceeded": result.exceeded,
        "probers_ran": list(result.probers_ran),
        "roots": [str(p) for p in result.roots],
        "attribution": [{"source": src, "path": str(p)} for src, p in result.attribution],
        "rejects": {
            "counts": counts,
            "sample": [{"reason": reason, "path": str(p)} for reason, p in result.rejects],
        },
        "errors": list(result.errors),
    }


def _manifest_form(backend: LocalBackend, device_id: str) -> str | None:
    """``shared`` / ``path`` from a manifest's first byte, ``None`` when unreadable."""
    try:
        raw = backend.get(manifest_key(device_id))
    except Exception:
        return None
    if not raw:
        return None
    if raw[0] == crypto.MANIFEST_FORMAT_VERSION:
        return "shared"
    return "path" if raw[0] == crypto.FORMAT_VERSION else None


def _project_index_state(
    cfg: dict,
    config_state: str,
    dev_id: str | None,
    backend: LocalBackend,
    resolved_sources: list[dict],
    inventory: Any,
) -> dict:
    """`mm diag --json`'s ``project_index`` (Track 90A). Needs no passphrase and
    names no peer device id: versions come from the plaintext registry and
    ``form`` from each manifest's first byte."""
    try:
        base: dict = {
            "state": None,
            "reason": None,
            "folders": 0,
            "decoded": 0,
            "owned": 0,
            "timed_out": False,
            "clones": [],
            "rows": [],
            "machines": [],
        }
        if config_state != "ok":
            return {**base, "reason": "config_invalid"}
        if not any(src.get("name") == "claude" for src in resolved_sources):
            return base
        core._configure_project_names(cfg)
        snapshot = projectid.diag_state()
        failure = projectid.home_failure()
        if failure is not None:
            snapshot["reason"] = "home_unresolved"
        elif not projectid.projects_listed():
            snapshot["reason"] = "projects_unlisted"
        own_form = _manifest_form(backend, dev_id) if dev_id else None
        snapshot["state"] = (
            None
            if snapshot["reason"] == "home_unresolved"
            else "shared"
            if own_form == "shared"
            else "path"
        )
        machines = []
        for device in getattr(inventory, "valid", []) or []:
            did = device.get("device_id")
            if not did or did == dev_id:
                continue
            version = device.get("last_seen_version")
            machines.append(
                {
                    "name": safety.safe_terminal_str(device.get("device_name", "")),
                    "version": version if isinstance(version, str) else None,
                    "form": _manifest_form(backend, did),
                }
            )
        snapshot["machines"] = machines
        return {**base, **snapshot}
    except Exception:
        return {
            "state": None,
            "reason": None,
            "folders": 0,
            "decoded": 0,
            "owned": 0,
            "timed_out": False,
            "clones": [],
            "rows": [
                {
                    "folder": "(unavailable)",
                    "form": "literal",
                    "identity": None,
                    "why": "unreadable",
                }
            ],
            "machines": [],
        }


def _fast_forward_state(sources: list[dict]) -> dict:
    """Read-only Track 91A state: pull-ledger health, rows per peer, scan-root tokens.

    Never writes or decrypts peer manifests. Older-edit details are historical
    observations from the last real pull, explicitly labeled as possibly stale.
    """
    ledger_state, peers, seen = "ok", {}, 0
    try:
        with statedb.connect(read_only=True) as conn:
            if conn is None:
                ledger_state = "absent"
            else:
                peers, seen = statedb.ledger_counts(conn)
    except (statedb.StateDBError, OSError) as exc:
        ledger_state = core.ledger_cause(exc) or "unreadable"
    anchors: dict[str, dict[str, str]] = {}
    for src in sources:
        if src.get("name") in MM_INTERNAL_SOURCE_NAMES:
            continue
        base = Path(src["path"]).expanduser()
        roots: dict[str, str] = {}
        for root in manifest.scan_roots(src):
            folder = base / root if root else base
            try:
                present = not manifest.path_has_descendant_symlink(folder, base) and stat.S_ISDIR(
                    folder.stat().st_mode
                )
            except OSError:
                present = False
            token = manifest.read_anchor(base, root) if present else None
            roots[root or "."] = (
                "missing folder" if not present else "token present" if token else "no token"
            )
        if roots:
            anchors[src["name"]] = roots
    return {
        "ledger": ledger_state,
        "peer_base_rows": peers,
        "seen_version_rows": seen,
        "anchors": anchors,
        "observations": syncdiag.read_observations(),
    }


def _syncthing_diag(storage_path: Path) -> dict[str, Any]:
    """Read-only Syncthing diagnostic state; never exposes credentials or peer IDs."""
    try:
        st = syncthing.read_state(storage_path, stignore_line=initflow.STIGNORE_LINE)
        return {
            "state": st.state,
            "cause": st.cause,
            "config_dir": (
                core._home_relative_path(st.config_dir) if st.config_dir is not None else None
            ),
            "gui_address": st.gui_address,
            "folder_id": st.folder_id,
            "folder_label": st.folder_label,
            "folder_path": (
                core._home_relative_path(st.folder_path) if st.folder_path is not None else None
            ),
            "folder_state": st.folder_state,
            "files_to_fetch": st.files_to_fetch,
            "bytes_to_fetch": st.bytes_to_fetch,
            "pull_errors": st.pull_errors,
            "connected_peers": st.connected_peers,
            "total_peers": st.total_peers,
            "options": dict(st.options),
            "problems": list(st.problems),
            "stignore": st.stignore,
        }
    except Exception as exc:
        return {"state": "error", "cause": type(exc).__name__}


def _print_syncthing_diag(st: dict[str, Any]) -> None:
    console.print("\n[bold]Syncthing[/bold]")
    st_state = st.get("state") or "unknown"
    cause = st.get("cause")
    state_shown = f"{st_state} ({cause})" if cause else str(st_state)
    console.print(f"  state:                   {safe_str(safety.safe_terminal_str(state_shown))}")
    if st.get("config_dir"):
        console.print(
            f"  config_dir:              {safe_str(safety.safe_terminal_str(st['config_dir']))}"
        )
    if st.get("gui_address"):
        console.print(
            f"  gui_address:             {safe_str(safety.safe_terminal_str(st['gui_address']))}"
        )
    folder_id = st.get("folder_id")
    folder_label = st.get("folder_label")
    folder_path = st.get("folder_path")
    problems = set(st.get("problems") or ())
    if folder_id or folder_path:
        label_part = (
            f"{folder_label} ({folder_id})"
            if folder_label and folder_id
            else (folder_id or folder_label or "")
        )
        path_part = f" at {folder_path}" if folder_path else ""
        folder_shown = f"{label_part}{path_part}".strip()
        console.print(
            f"  folder:                  {safe_str(safety.safe_terminal_str(folder_shown))}"
        )
    elif "no-folder" in problems:
        console.print(
            "  folder:                  none shares the storage path (problem: no-folder)"
        )
    if st.get("folder_state") is not None or "paused" in problems:
        fstate = st.get("folder_state") or ("paused" if "paused" in problems else "unknown")
        fetch_files = st.get("files_to_fetch")
        fetch_bytes = st.get("bytes_to_fetch")
        extra = ""
        if fetch_files:
            extra = f" ({fetch_files} files, {fetch_bytes or 0} bytes to fetch)"
        if "paused" in problems and fstate != "paused":
            extra += " (paused)"
        console.print(
            f"  folder state:            {safe_str(safety.safe_terminal_str(f'{fstate}{extra}'))}"
        )
    if st.get("pull_errors"):
        console.print(f"  pull errors:             {st['pull_errors']}")
    conn = st.get("connected_peers")
    total = st.get("total_peers")
    if total is not None:
        conn_shown = "unknown" if conn is None else str(conn)
        console.print(f"  connected peers:         {conn_shown} of {total}")
    options = st.get("options") or {}
    if options:
        for opt_name, wanted, why in syncthing.FOLDER_OPTIONS:
            raw_val = options.get(opt_name)
            if opt_name == "versioning":
                val_shown = str(raw_val) if raw_val else "none"
            elif isinstance(raw_val, bool):
                val_shown = "true" if raw_val else "false"
            elif raw_val is None:
                val_shown = "unknown"
            else:
                val_shown = str(raw_val)
            if syncthing.option_is_ok(opt_name, raw_val):
                verdict = "(ok)"
            else:
                verdict = f"(want {wanted}: {why})"
            console.print(
                safe_str(safety.safe_terminal_str(f"  option {opt_name}: {val_shown} {verdict}"))
            )
    stignore = st.get("stignore")
    if stignore is not None:
        if stignore == "ok":
            st_detail = f"ok ({initflow.STIGNORE_LINE})"
        elif stignore == "missing":
            st_detail = f"missing (want {initflow.STIGNORE_LINE} in .stignore)"
        elif stignore == "missing-line":
            st_detail = f"missing line (want {initflow.STIGNORE_LINE} in .stignore)"
        else:
            st_detail = f"unreadable (want {initflow.STIGNORE_LINE} in .stignore)"
        console.print(f"  .stignore:               {safe_str(safety.safe_terminal_str(st_detail))}")
    console.print("  mm only reads Syncthing; change these settings in Syncthing.")


def _collect_diag_state(backend: LocalBackend) -> dict:
    """Gather non-secret state for support triage.

    Secrets allowlist — NEVER include:
      * raw root_salt bytes (fingerprint only)
      * master_key (never computed here)
      * keycheck_blob contents
      * passphrase
      * peer device_ids (only counts)
      * host_usage values other than the cache and path-count diag keys: Codex's
        cache_state / state /
        files_cached / files_migrated / files_pre_track / files_on_disk /
        pending / model_count / models / last_reason / last_reason_since, and
        Cursor's consented / complete_once / cache_state / runs_cached /
        model_count / models / last_reason / last_reason_since / hook_state /
        pending_completions / unread_sqlite_stores / unsupported_sqlite_stores
        ({count, causes}) / last_reason_detail ({cause, store, file?, code?}: a
        closed cause vocabulary, a 16-hex store id, a store file name and an
        SQLite or OS error name; never a path, a row or an identifier of a run);
        every reader also exposes last_complete_ms / last_complete_at /
        last_deadline_allotted_ms. Top-level host_read_budgets contains only
        autopush_ms / autopush_source / interactive_ms / interactive_source /
        capture_ms / capture_source / warm_ms
        (never a path, never a host store,
        never a token magnitude)
      * local_emails (this machine's author-email trust set, and peers'
        after a pull merge) — project an allowlist, never render the row
      * host_publication payloads: only state, timestamp/age, allowlisted
        reader coverage, row-level empty and nullable empty_readers,
        publication/publication_reason, latest_attempt class/cause/time/readers/
        read-reason/superseded evidence and sanitized errors
        may escape; never hosts, tokens_by_day, magnitudes, models or peer ids
      * keyring values other than the module-qualified backend/member class
        paths, the closed secret_service / item vocabularies and an
        exception class name; never a secret, an item label or an item path
        (Track 73A)
      * Syncthing GUI credential, Syncthing device IDs, or Syncthing device
        names (only state, cause, home-relative config/folder paths, GUI
        address, folder id/label/state, fetch counts, peer counts, option
        values, problems and .stignore state; Track 92A)

    Uses existing tri-state helpers (`fetch_crypto_init`, `sidecar.read`)
    rather than re-sampling raw blob bytes — the tri-state branches are
    where corruption meaning lives, so bypassing them would misreport the
    exact scenario this command exists to diagnose.
    """
    # Local config (best-effort — a broken config is itself diag-worthy).
    skill_may_create: frozenset[str] | None = None
    skill_config_error: str | None = None
    resolved_sources: list[dict] = []
    selected_sources: list[dict] | None = None
    cfg: dict = {}
    try:
        cfg = load_config()
        dev_id = cfg.get("device", {}).get("id")
        dev_name = cfg.get("device", {}).get("name")
        storage_path = cfg.get("storage", {}).get("path")
        local_fp = cfg.get("crypto", {}).get("root_salt_fp")
        config_state = "ok"
        source_resolution = resolve_sources(cfg, strict=False, bootstrap=False)
        resolved_sources = source_resolution.available
        selected_sources = source_resolution.selected
        skill_may_create = skill_link.consented_agent_keys(cfg, resolved_sources)
    except MindMeldError as e:
        dev_id = dev_name = storage_path = local_fp = None
        config_state = f"error: {e}"
        skill_config_error = str(e)
        skill_may_create = frozenset()

    # Storage crypto init (delegates tri-state to the source of truth).
    fetch = fetch_crypto_init(backend)
    if fetch.status == "ok":
        crypto_init = {
            "status": "ok",
            "root_salt_fp": root_salt_fingerprint(fetch.root_salt),
            "argon2_memory_kb": fetch.argon2_memory_kb,
            "pending_repair": crypto.crypto_init_repair_counts(fetch),
        }
    else:
        crypto_init = {"status": fetch.status}
    crypto_init["newer_version"] = fetch.newer_version

    # Sidecar (scoped by local device_id for device_id-mismatch detection).
    sidecar_info: dict = {"path": str(sidecar.sidecar_path())}
    try:
        sc = sidecar.read(dev_id) if dev_id else None
        if sc is None and sidecar.sidecar_path().exists():
            # sidecar exists on disk but read() returned None (device_id mismatch,
            # shape-invalid, etc.) — useful triage signal.
            sidecar_info["state"] = "present_but_rejected"
        elif sc is None:
            sidecar_info["state"] = "missing"
        else:
            sidecar_info["state"] = "ok"
            sidecar_info["device_id"] = sc.get("device_id")
            sidecar_info["timestamp"] = sc.get("timestamp")
    except Exception as e:  # very defensive — diag must not crash
        sidecar_info["state"] = f"error: {type(e).__name__}"

    # Storage inventory — counts only, no identifiers. The manifest and device
    # totals count valid keys only (Track 87A): a conflict copy, a misnamed file
    # or a sync tool's temporary file is never a manifest or a device, and is
    # counted separately as ignored.
    def _is_manifest_key(k: str) -> bool:
        parts = k.split("/")
        return len(parts) == 3 and parts[2] == "manifest.json.enc"

    def _count(prefix: str, valid: Callable[[str], bool]) -> int:
        try:
            return sum(1 for k in backend.list_keys(prefix) if valid(k))
        except Exception:
            return -1  # signals "could not enumerate"

    # For manifests/ and data/, count per-device sub-prefixes as "peer count."
    def _count_device_prefixes(prefix: str) -> int:
        try:
            prefixes = set()
            for k in backend.list_keys(prefix):
                parts = k.split("/")
                if len(parts) >= 3:
                    prefixes.add(parts[1])
            return len(prefixes)
        except Exception:
            return -1

    # One read of devices/ feeds both the device total and the ignored count.
    try:
        device_inventory = inventory_devices(backend)
    except Exception:
        device_inventory = None

    def _ignored_files() -> int:
        if device_inventory is None:
            return -1
        try:
            # Foreign names anywhere, plus entries in devices/ that are not a
            # device (conflict copies, misnamed files, an id that is not the
            # file name).
            return len(backend.list_foreign_keys()) + len(device_inventory.foreign)
        except Exception:
            return -1

    # Own manifest conflict copies via the existing find_conflict_copies
    # helper, if a local device_id is known.
    own_conflict_copies = -1
    if dev_id:
        try:
            # Predicate is validator-free because we don't need to decrypt;
            # count all sibling files that match the conflict-name pattern.
            own_conflict_copies = len(
                backend.find_conflict_copies(manifest_key(dev_id), lambda p: True)
            )
        except Exception:
            pass

    storage_inv = {
        "manifests_total": _count(MANIFESTS_PREFIX, _is_manifest_key),
        "manifest_peer_count": _count_device_prefixes(MANIFESTS_PREFIX),
        "data_peer_count": _count_device_prefixes(DATA_PREFIX),
        "devices_total": (
            -1
            if device_inventory is None
            else len(device_inventory.valid)
            + len(device_inventory.dropped)
            + len(device_inventory.vanished)
        ),
        "own_manifest_conflict_copies": own_conflict_copies,
        "ignored_storage_files": _ignored_files(),
    }

    # Last autorun breadcrumb (ops-oriented: when did autopush/autopull
    # last fire and how did it end). Keyed per verb since v0.12.45 so
    # autopull cannot erase a degraded autopush crumb.
    auto_evidence = _read_autorun_evidence()
    breadcrumb = (
        None
        if auto_evidence["state"] == "missing"
        else auto_evidence["runs"]
        if auto_evidence["state"] == "valid"
        else {"error": "unreadable"}
    )

    discovery = _collect_discovery_diag(cfg)
    from mind_meld import host_usage as _host_usage

    muse_consented = (
        any(source.get("name") == "muse" for source in resolved_sources)
        if config_state == "ok"
        else None
    )
    agy_consented = (
        any(source.get("name") == "agy" for source in resolved_sources)
        if config_state == "ok"
        else None
    )
    grok_consented = _config_module.grok_usage_consented(cfg) if config_state == "ok" else None
    host_usage_state = {
        name: (
            _host_usage.muse_usage_diag(count_logs=muse_consented is True)
            if name == "muse"
            else _host_usage.agy_usage_diag(count_logs=agy_consented is True)
            if name == "agy"
            else host_registry.reader_usage_diag(name, count_logs=grok_consented is True)
            if name == "grok"
            else host_registry.reader_usage_diag(name)
        )
        for name in host_registry.HOST_READER_DIAGS
    }
    host_usage_state["muse"]["consented"] = muse_consented
    host_usage_state["agy"]["consented"] = agy_consented
    host_usage_state["grok"]["consented"] = grok_consented
    host_usage_state["cursor"]["consented"] = (
        _config_module.cursor_host_usage_enabled(cfg) if config_state == "ok" else None
    )
    capture_rows = _read_capture_rows(resolved_sources, dev_id)
    attended_evidence = attemptlog.read_runs()
    run_projection = attemptlog.project_runs(auto_evidence, attended_evidence)
    return {
        "mm_version": __version__,
        "config": {
            "state": config_state,
            "device_id": dev_id,
            "device_name": dev_name,
            "storage_path": storage_path,
            "root_salt_fp": local_fp,  # fingerprint only, never the raw salt
        },
        "crypto_init": crypto_init,
        "keyring": crypto._secret_service_state(),
        "root_salt_drift": (
            "ok"
            if (local_fp and crypto_init.get("root_salt_fp") == local_fp)
            else "mismatch"
            if (local_fp and crypto_init.get("status") == "ok")
            else "n/a"
        ),
        "sidecar": sidecar_info,
        "storage_inventory": storage_inv,
        "syncthing": _syncthing_diag(_backend_storage_path(backend, cfg)),
        "project_index": _project_index_state(
            cfg, config_state, dev_id, backend, resolved_sources, device_inventory
        ),
        "sync_scope": {
            "skipped_nested_repositories": {
                src["name"]: manifest.nested_repo_skip_prefixes(src) for src in resolved_sources
            }
            if config_state == "ok"
            else None,
        },
        "fast_forward": _fast_forward_state(resolved_sources if config_state == "ok" else []),
        "last_autorun": breadcrumb,
        "last_attended_runs": attended_evidence,
        "sync_run_projection": run_projection,
        "skill_links": skill_link.diagnose_skill_links(
            may_create=skill_may_create, config_error=skill_config_error
        ),
        "host_usage": host_usage_state,
        "host_read_budgets": _host_read_budgets(cfg if config_state == "ok" else None),
        "host_publication": _host_publication(
            cfg, resolved_sources, capture_rows, selected=selected_sources
        ),
        "discovery": discovery,
        "git_capture": _collect_git_capture_diag(cfg, dev_id, discovery, scan=capture_rows),
    }


def diag(
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of plain text."
    ),
) -> None:
    """Dump non-secret crypto + sync state for support triage.

    Never prints raw root_salt, master_key, keycheck, passphrase, or peer
    device_ids. See SPEC.md "Manifest corruption recovery" for how this
    maps to the tri-state recovery chain — each field here names one
    possible cause of a recovery failure.
    """
    # Resolve storage path WITHOUT a valid config: diag has to run even
    # when config is broken (that's literally one of the things users run
    # it to debug). Fall back to the default storage path.
    cfg = None
    try:
        cfg = load_config()
        storage_path = cfg["storage"]["path"]
    except MindMeldError:
        storage_path = DEFAULT_STORAGE_PATH
    full_path = Path(storage_path).expanduser()
    backend = LocalBackend(str(full_path))

    state = _collect_diag_state(backend)

    if as_json:
        import json as _json

        # Emit via typer.echo, NOT console.print: Rich would reflow the JSON
        # (word-wrap, style markup) and break downstream consumers.
        typer.echo(_json.dumps(state, indent=2, sort_keys=True, default=str))
        return

    # Plain text formatting — optimized for support-chat paste.
    console.print(f"[bold]Mind Meld diag (v{state['mm_version']})[/bold]\n")

    cfg_state = state["config"]
    console.print("[bold]Config[/bold]")
    console.print(f"  state:         {cfg_state['state']}")
    console.print(f"  device_id:     {cfg_state['device_id'] or '(none)'}")
    console.print(f"  device_name:   {cfg_state['device_name'] or '(none)'}")
    console.print(f"  storage_path:  {cfg_state['storage_path'] or '(default)'}")
    console.print(f"  root_salt_fp:  {cfg_state['root_salt_fp'] or '(unset)'}")
    scope = state["sync_scope"]["skipped_nested_repositories"]
    if scope is None:
        console.print("  nested repository skips: unknown (config unavailable)")
    else:
        for name, roots in scope.items():
            for root in roots:
                console.print(
                    safe_str(
                        "  skipped: nested git repository "
                        + safety.safe_terminal_str(f"{name}:{root}")
                    )
                )

    ff = state["fast_forward"]
    console.print("\n[bold]Fast-forward ledger[/bold]")
    console.print(f"  state:         {ff['ledger']}")
    console.print(f"  seen versions: {ff['seen_version_rows']}")
    for peer, count in sorted(ff["peer_base_rows"].items()):
        console.print(f"  last-synced files from {safe_str(peer)}: {count}")
    for name, roots in ff["anchors"].items():
        for root, status in roots.items():
            console.print(safe_str(f"  scan root {name}:{root}: {status}"))
    observations = ff["observations"]
    last_pull = observations["pull"]
    if last_pull is not None:
        completeness = "complete" if last_pull["complete"] else "partial"
        console.print(
            safe_str(
                f"  Last pull observation: {last_pull['observed_at']} ({completeness}; "
                "may be stale; peer files not rechecked)"
            )
        )
        for row in last_pull["older"]:
            console.print(
                safe_str(
                    f"  older peer edit not applied: {row['peer']}/{row['source']}/{row['path']}"
                )
            )
            console.print(
                safe_str(
                    f"    local mtime: {row['local_mtime'] or 'unknown'}; "
                    f"peer mtime: {row['peer_mtime'] or 'unknown'}"
                )
            )
        if last_pull["older_count"] > len(last_pull["older"]):
            console.print(
                f"  Showing {len(last_pull['older'])} of {last_pull['older_count']} older edits."
            )
        if last_pull["older"]:
            console.print(
                "  Compare both versions first; touch the file and run mm push "
                "on the machine whose version should win."
            )
    elif observations["state"] != "missing":
        console.print(safe_str(f"  sync observations: {observations['state']}"))
    for row in observations["push"]["records"]:
        if row["count"] >= 2:
            console.print(
                safe_str(
                    f"  repeated anchor mismatch: {row['source']}:{row['root'] or '.'} "
                    f"({row['count']} accepted pushes; last observed {row['observed_at']}; "
                    "may be stale)"
                )
            )

    pi = state["project_index"]
    names_state = pi.get("state") or "n/a"
    summary = (
        f"{names_state}; {pi['folders']} folders, {pi['decoded']} decoded, {pi['owned']} owned"
    )
    if pi.get("reason"):
        summary += f" ({pi['reason']})"
    if pi.get("timed_out"):
        summary += "; index timed out"
    console.print(f"  project names: {safe_str(summary)}")

    ci = state["crypto_init"]
    console.print("\n[bold]mm-crypto-init[/bold]")
    console.print(f"  status:        {ci.get('status')}")
    console.print(f"  newer_version: {ci.get('newer_version')}")
    if ci.get("newer_version") is not None:
        console.print(safe_str(core._newer_format_message(ci["newer_version"])))
    if ci.get("status") == "ok":
        console.print(f"  root_salt_fp:  {ci.get('root_salt_fp')}")
        console.print(f"  argon2 mem kb: {ci.get('argon2_memory_kb')}")
    console.print(f"  drift check:   {state['root_salt_drift']}")

    kr_state = state["keyring"]
    console.print("\n[bold]Keyring[/bold]")
    console.print(f"  backend:        {kr_state.get('backend')}")
    console.print(f"  member:         {kr_state.get('member')}")
    console.print(f"  secret_service: {kr_state.get('secret_service')}")
    console.print(f"  item:           {kr_state.get('item')}")
    console.print(f"  error:          {kr_state.get('error')}")

    sc = state["sidecar"]
    console.print("\n[bold]Sidecar (local last-push snapshot)[/bold]")
    console.print(f"  path:          {sc.get('path')}")
    console.print(f"  state:         {sc.get('state')}")
    if sc.get("device_id"):
        console.print(f"  device_id:     {sc.get('device_id')}")
    if sc.get("timestamp"):
        console.print(f"  timestamp:     {sc.get('timestamp')}")

    inv = state["storage_inventory"]
    console.print("\n[bold]Storage inventory[/bold]")
    console.print(f"  manifests total:         {inv['manifests_total']}")
    console.print(f"  manifest peer count:     {inv['manifest_peer_count']}")
    console.print(f"  data peer count:         {inv['data_peer_count']}")
    console.print(f"  devices/ entries:        {inv['devices_total']}")
    console.print(f"  own manifest conflicts:  {inv['own_manifest_conflict_copies']}")
    console.print(f"  ignored storage files:   {inv['ignored_storage_files']}")

    _print_syncthing_diag(state.get("syncthing") or {})

    console.print("\n[bold]Last autorun[/bold]")
    br = state["last_autorun"]
    if br is None:
        console.print("  (no autopull/autopush has run on this device yet)")
    elif "error" in br:
        console.print("  [yellow]breadcrumb unreadable[/yellow]")
    else:
        rendered_any = False
        for verb in ("pull", "push"):
            entry = br.get(verb)
            if not isinstance(entry, dict):
                continue
            rendered_any = True
            console.print(f"  {verb}:")
            console.print(f"    outcome:    {safe_str(str(entry.get('outcome', '')))}")
            console.print(f"    timestamp:  {safe_str(str(entry.get('timestamp', '')))}")
            if entry.get("detail"):
                # safe_str at the render site covers every producer at once:
                # sibling `_write_autorun_breadcrumb(verb, "failed"/"config-error",
                # str(e))` calls can carry peer-derived text (device names, source
                # names, rel_paths from a peer manifest) into this field, and Rich
                # interprets markup in an f-string. The v0.12.16 degradation
                # strings are all literals, but the field is shared.
                console.print(f"    detail:     {safe_str(str(entry.get('detail')))}")
        if not rendered_any:
            console.print("  [yellow]breadcrumb unreadable[/yellow]")

    console.print("\n[bold]Last attended runs[/bold]")
    console.print(
        safe_str(safety.safe_terminal_str(json.dumps(state["last_attended_runs"], sort_keys=True)))
    )
    console.print("\n[bold]Sync run projection[/bold]")
    console.print(
        safe_str(safety.safe_terminal_str(json.dumps(state["sync_run_projection"], sort_keys=True)))
    )

    console.print("\n[bold]Skill links[/bold]")
    for row in state.get("skill_links") or []:
        agent = safe_str(str(row.get("agent", "")))
        status = safe_str(str(row.get("status", "")))
        target = safe_str(str(row.get("target", "")))
        detail = row.get("readlink") or row.get("detail") or row.get("store_state")
        extra = f" ({safe_str(str(detail))})" if detail else ""
        policy = row.get("maintain_links") or ""
        console.print(f"  {agent}: {status}{extra}")
        if policy:
            console.print(f"    maintain_links: {safe_str(str(policy))}")
        if status not in ("ok", "absent") and target:
            console.print(f"    {target}")

    console.print("\n[bold]Host usage[/bold]")
    publication = state["host_publication"]
    try:
        # The cached view only: no fetch, no cache write. diag runs on broken
        # state, so any failure keeps the `mm update` remedy.
        upgrade_view = upgrade.cached_upgrade_view(cfg)
    except Exception:
        upgrade_view = None
    no_release = _no_release_readers(publication, state["host_usage"], upgrade_view)
    upgrade_required = _usage_capture_needs_upgrade(publication, state["host_usage"], upgrade_view)
    _print_host_publication(
        publication, reader_states=state["host_usage"], upgrade_view=upgrade_view
    )
    budgets = state["host_read_budgets"]
    if budgets["autopush_ms"] is None:
        budget_line = "unknown (config unavailable)"
    else:
        budget_line = (
            f"autopush {budgets['autopush_ms']} ms ({budgets['autopush_source']}) · "
            f"interactive {budgets['interactive_ms']} ms ({budgets['interactive_source']}) · "
            f"capture {budgets['capture_ms']} ms ({budgets['capture_source']})"
        )
    console.print("  host read budgets: " + safe_str(budget_line))
    console.print("  host read sweep estimate: " + safe_str(_host_read_sweep_line(state)))

    mu_state = (state.get("host_usage") or {}).get("muse") or {}
    mu_consented = mu_state.get("consented")
    if mu_consented is None:
        mu_consented_shown = "(config unreadable)"
    else:
        mu_consented_shown = "yes" if mu_consented else "no"
    if mu_state.get("complete_once"):
        mu_scan_shown = "yes"
    elif mu_state.get("cache_state") == "unreadable":
        mu_scan_shown = "unreadable"
    else:
        mu_scan_shown = "no"
    console.print(f"  muse consented:          {mu_consented_shown}")
    console.print(f"  muse prior successful scan: {mu_scan_shown}")
    console.print(f"  muse cache inventory:    {safe_str(str(mu_state.get('cache_state', '')))}")
    if mu_state.get("cache_state") == "ok":
        console.print(
            "  muse usage read blocker: "
            + _host_usage_blocker(
                "muse",
                mu_state,
                in_diag=True,
                readiness=_reader_capture_readiness(publication, "muse"),
                upgrade_required=upgrade_required,
                no_release_reads=no_release,
            )
        )
    mu_cached = mu_state.get("files_cached")
    mu_disk = mu_state.get("files_on_disk")
    console.print(
        f"  muse session logs cached: {'unknown' if mu_cached is None else mu_cached}"
        f" of {'unknown' if mu_disk is None else mu_disk}"
    )
    console.print(f"  muse models cached:      {_diag_models_line(mu_state)}")
    console.print("  muse last complete read: " + safe_str(_host_complete_read_line(mu_state)))

    agy_state = (state.get("host_usage") or {}).get("agy") or {}
    agy_consented = agy_state.get("consented")
    if agy_consented is None:
        agy_consented_shown = "(config unreadable)"
    else:
        agy_consented_shown = "yes" if agy_consented else "no"
    if agy_state.get("complete_once"):
        agy_scan_shown = "yes"
    elif agy_state.get("cache_state") == "unreadable":
        agy_scan_shown = "unreadable"
    else:
        agy_scan_shown = "no"
    console.print(f"  agy consented:           {agy_consented_shown}")
    console.print(f"  agy prior successful scan: {agy_scan_shown}")
    console.print(f"  agy cache inventory:     {safe_str(str(agy_state.get('cache_state', '')))}")
    if agy_state.get("cache_state") == "ok":
        console.print(
            "  agy usage read blocker:  "
            + _host_usage_blocker(
                "agy",
                agy_state,
                in_diag=True,
                readiness=_reader_capture_readiness(publication, "agy"),
                upgrade_required=upgrade_required,
                no_release_reads=no_release,
            )
        )
    agy_cached = agy_state.get("files_cached")
    agy_disk = agy_state.get("files_on_disk")
    console.print(
        f"  agy conversation DBs cached: {'unknown' if agy_cached is None else agy_cached}"
        f" of {'unknown' if agy_disk is None else agy_disk}"
    )
    console.print(f"  agy models cached:       {_diag_models_line(agy_state)}")
    console.print("  agy last complete read:  " + safe_str(_host_complete_read_line(agy_state)))

    grok_state = (state.get("host_usage") or {}).get("grok") or {}
    grok_consented = grok_state.get("consented")
    if grok_consented is None:
        grok_consented_shown = "(config unreadable)"
    else:
        grok_consented_shown = "yes" if grok_consented else "no"
    if grok_state.get("complete_once"):
        grok_scan_shown = "yes"
    elif grok_state.get("cache_state") == "unreadable":
        grok_scan_shown = "unreadable"
    else:
        grok_scan_shown = "no"
    console.print(f"  grok consented:          {grok_consented_shown}")
    console.print(f"  grok prior successful scan: {grok_scan_shown}")
    console.print(f"  grok cache inventory:    {safe_str(str(grok_state.get('cache_state', '')))}")
    if grok_state.get("cache_state") == "ok":
        console.print(
            "  grok usage read blocker: "
            + _host_usage_blocker(
                "grok",
                grok_state,
                in_diag=True,
                readiness=_reader_capture_readiness(publication, "grok"),
                upgrade_required=upgrade_required,
                no_release_reads=no_release,
            )
        )
    grok_cached = grok_state.get("files_cached")
    grok_disk = grok_state.get("files_on_disk")
    console.print(
        f"  grok sessions cached:    {'unknown' if grok_cached is None else grok_cached}"
        f" of {'unknown' if grok_disk is None else grok_disk}"
    )
    console.print(f"  grok sessions without usage: {grok_state.get('usage_less_skipped', 0)}")
    console.print(f"  grok models cached:      {_diag_models_line(grok_state)}")
    console.print("  grok last complete read: " + safe_str(_host_complete_read_line(grok_state)))

    cx_state = (state.get("host_usage") or {}).get("codex") or {}
    console.print(f"  codex cache:             {safe_str(str(cx_state.get('cache_state', '')))}")
    console.print(f"  codex cache inventory:   {safe_str(str(cx_state.get('state', '')))}")
    if cx_state.get("cache_state") == "ok":
        console.print(
            "  codex usage read blocker: "
            + _host_usage_blocker(
                "codex",
                cx_state,
                in_diag=True,
                readiness=_reader_capture_readiness(publication, "codex"),
                upgrade_required=upgrade_required,
                no_release_reads=no_release,
            )
        )
    cx_disk = cx_state.get("files_on_disk")
    console.print(
        f"  codex rollouts cached:   {cx_state.get('files_cached', 0)}"
        f" of {'unknown' if cx_disk is None else cx_disk}"
    )
    console.print(f"  codex models cached:     {_diag_models_line(cx_state)}")
    console.print("  codex last complete read: " + safe_str(_host_complete_read_line(cx_state)))
    if cx_state.get("files_pre_track") and not cx_state.get("last_reason"):
        # The actionable half: these entries predate per-turn accounting and
        # are re-walked once. Autopush never warms the host cache, so on a
        # quiet Mac the nudge is the only way a user learns to finish it.
        console.print(
            f"  codex awaiting re-walk:  {cx_state['files_pre_track']}"
            " — "
            + safe_str(
                _usage_capture_remedy(publication, state["host_usage"], upgrade_view)
                if "codex" in publication["readers"]
                else _config_module.usage_capture_remedy(
                    _reader_capture_readiness(publication, "codex"), reader="codex"
                )
            )
        )

    cursor_state = state["host_usage"].get("cursor", {})
    console.print(f"  {host_registry.HOST_READER_DIAGS['cursor'].label}:")
    consent = cursor_state.get("consented")
    console.print(
        "    consented: " + ("unknown" if consent is None else "yes" if consent else "no")
    )
    console.print("    completion hook: " + safe_str(cursor_state.get("hook_state", "unknown")))
    pending = cursor_state.get("pending_completions")
    console.print(
        "    pending completions: "
        + ("unknown" if pending is None else f"{pending} (merged by the next push)")
    )
    unread = cursor_state.get("unread_sqlite_stores")
    console.print(
        "    Conductor workspace stores not yet read: "
        + ("unknown" if unread is None else str(unread))
    )
    unsupported = cursor_state.get("unsupported_sqlite_stores")
    if isinstance(unsupported, dict) and unsupported.get("count"):
        console.print("    " + safe_str(_cursor_unsupported_clause(unsupported)))
    console.print("    cache inventory: " + safe_str(cursor_state.get("cache_state", "unknown")))
    if cursor_state.get("cache_state") == "ok" or cursor_state.get("last_reason"):
        console.print(
            "    usage read blocker: "
            + _host_usage_blocker(
                "cursor",
                cursor_state,
                in_diag=True,
                readiness=_reader_capture_readiness(publication, "cursor"),
                upgrade_required=upgrade_required,
                no_release_reads=no_release,
            )
        )
        detail_clause = _cursor_store_detail_clause(cursor_state.get("last_reason_detail"))
        if detail_clause:
            console.print("    store detail: " + safe_str(detail_clause))
    retained = cursor_state.get("runs_cached")
    console.print(f"    runs retained: {'unknown' if retained is None else retained}")
    console.print("    models cached: " + _diag_models_line(cursor_state))
    console.print("    last complete read: " + safe_str(_host_complete_read_line(cursor_state)))

    disc = state.get("discovery") or {}
    console.print("\n[bold]Git-root discovery[/bold] (autopush budget)")
    console.print(f"  budget_ms:    {disc.get('budget_ms')}")
    console.print(f"  elapsed_ms:   {disc.get('elapsed_ms')}")
    console.print(f"  status:       {safe_str(str(disc.get('status', '')))}")
    if disc.get("status") == "error":
        console.print(f"  error:        {safe_str(str(disc.get('error', '')))}")
    probers = disc.get("probers_ran") or []
    if not probers:
        console.print("  probers:      (none ran — no provider enabled or tool installed)")
    else:
        console.print(f"  probers:      {safe_str(', '.join(str(p) for p in probers))}")
    roots = disc.get("roots") or []
    console.print(f"  roots:        {len(roots)}")
    found_by = {
        str(item.get("path")): str(item.get("source"))
        for item in disc.get("attribution") or []
        if isinstance(item, dict)
    }
    for raw in roots:
        shown = core._home_relative_path(Path(str(raw)))
        source = found_by.get(str(raw))
        suffix = f" ({safe_str(source)})" if source else ""
        console.print(f"    {safe_str(shown)}{suffix}")
    counts = (disc.get("rejects") or {}).get("counts") or {}
    parts = [
        f"{counts[reason]} {reason}" for reason in _DISCOVERY_REJECT_REASONS if counts.get(reason)
    ]
    console.print(f"  rejects:      {', '.join(parts) if parts else '0'}")
    if disc.get("errors"):
        console.print(f"  errors:       {len(disc['errors'])}")
        for err in disc["errors"]:
            console.print(f"    {safe_str(str(err))}")

    cap = state.get("git_capture") or {}
    console.print("\n[bold]Git capture[/bold]")
    recorded = cap.get("recorded")
    if not recorded:
        console.print("  recorded:     (none — no mm-push row yet)")
    else:
        console.print("  recorded:")
        console.print(f"    ts:                  {recorded.get('ts') or '(none)'}")
        console.print(f"    mm_version:          {recorded.get('mm_version') or '(none)'}")
        console.print(
            f"    discovery:           {recorded.get('discovery') or '(legacy — no key)'}"
        )
        console.print(f"    since:               {recorded.get('since') or '(none)'}")
        console.print(f"    walk_budget_aborts:  {recorded.get('walk_budget_aborts')}")
        console.print(f"    walk_errors:         {recorded.get('walk_errors')}")
        console.print(f"    advances_cursor:     {recorded.get('advances_cursor')}")
    fresh = cap.get("fresh") or {}
    console.print("  fresh:")
    console.print(f"    discovery:           {fresh.get('discovery')}")
    console.print(f"    status:              {safe_str(str(fresh.get('status', '')))}")
    console.print(f"    roots:               {fresh.get('roots')}")
    console.print(f"    pending_repos:       {fresh.get('pending_repos')}")
    console.print(f"    exceeded:            {fresh.get('exceeded')}")
    console.print(f"    error_count:         {fresh.get('error_count')}")


_BREADCRUMB_STALE_AFTER_HOURS = 48


def _breadcrumb_staleness_suffix(ts: object) -> str:
    """Return a ``[yellow]stale[/yellow]`` marker when the breadcrumb is old.

    ``mm status`` renders the last autorun breadcrumb with no age check, so a
    device whose `mm autopull` / `mm autopush` stopped running entirely reports
    its last ``success`` indefinitely. Every other degradation signal mm has is
    written BY the command; this is the one case where nothing runs to write
    anything, which is exactly what a module-scope ``ImportError`` looks like.

    Best-effort: an unparseable or missing timestamp yields no marker rather
    than an error — the breadcrumb is diagnostics, not a correctness gate.
    """
    if not isinstance(ts, str):
        return ""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - when).total_seconds() / 3600.0
    if age_h < _BREADCRUMB_STALE_AFTER_HOURS:
        return ""
    return f" [yellow]stale — no autorun in {int(age_h)}h[/yellow]"


def _read_autorun_evidence() -> dict:
    evidence = attemptlog.read_run_file(core._autorun_breadcrumb_path())
    if evidence["state"] == "valid":
        raw = evidence["runs"]
        normalized = core._normalize_autorun_breadcrumbs(raw)
        if not normalized or any(
            verb in raw and verb not in normalized for verb in core._AUTORUN_VERBS
        ):
            evidence["state"] = "corrupt"
        evidence["runs"] = normalized
    return evidence


def _read_autorun_breadcrumbs() -> dict[str, dict]:
    """Current or legacy raw entries, without waiting or creating anything."""
    return _read_autorun_evidence()["runs"]
