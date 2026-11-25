"""Single-flight detached background capture catch-up (Track 165C).

Extracted module: never imports ``cli``, ``core``, or any ``*flow`` module.
Runs capture-only catch-up after a deadline-stopped ``mm autopush``:
  - Step 1 (under L1 ``capture-catchup.lock``, outside L2 ``mind-meld.lock``):
    warms cold or deadline-stopped private host and Claude token caches with
    attended budgets and per-reader/per-dir consent rechecks.
  - Step 2 (under L1 -> L2 ``mind-meld.lock`` with bounded ~2 s retry):
    reloads config fresh, verifies ``git-capture-cache.json`` is valid or
    missing, runs single-phase attended capture, appends local
    ``git-snapshot(origin="catchup")``, ``sessions-snapshot``, and
    ``host-usage-snapshot`` rows to ``mm-events`` (no ``mm-push`` row),
    confirms the written Git snapshot on disk before advancing ``repo_cursors``,
    hashes the appended day file, stamps ``finished_at`` before releasing L2,
    and leaves encrypted publication to the next push.

Global lock hierarchy is strictly ``L1 < L2 < L3``:
  L1: ``capture-catchup.lock``
  L2: ``mind-meld.lock``
  L3: individual ``lockedjson`` files
"""

from __future__ import annotations

import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from mind_meld import (
    config,
    errors,
    events,
    events_tail,
    host_registry,
    lockedjson,
    lockfile,
    manifest,
    pushtimings,
    safety,
    token_usage,
)

CATCHUP_LOCK_PATH = config.CONFIG_DIR / "capture-catchup.lock"
CATCHUP_STATE_PATH = config.CONFIG_DIR / "capture-catchup.json"
CATCHUP_RECENT_WINDOW = timedelta(hours=24)
VALID_CATCHUP_STATES = frozenset({"idle", "running", "finished"})
VALID_CATCHUP_RESULTS = frozenset({"ok", "partial", "deferred", "no-sources", "failed"})
_SYNC_LOCK_BACKOFFS_S = (0.05, 0.1, 0.2, 0.4, 0.6, 0.65)
_STATE_LOCK_BACKOFFS_S = (0.005, 0.01, 0.02, 0.05)
_FUTURE_SKEW_WINDOW = timedelta(seconds=60)
_MAX_TRIGGER_PHASES = 16


class _ConfigChanged(Exception):
    """Raised when live config.toml changes mid-catch-up."""


@dataclass(frozen=True)
class _WarmedCaches:
    timed_hosts: frozenset[str] = frozenset()
    no_sources: bool = False
    config_stamp: tuple[Any, ...] = ()
    deadline_evidence: Mapping[str, events_tail.HostReadEvidence] = field(default_factory=dict)


def _empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "state": "idle",
        "result": None,
        "started_at": None,
        "finished_at": None,
        "trigger_phases": [],
        "event_file": None,
        "event_sha256": None,
        "detail": None,
    }


def _normalize_phases(phases: Sequence[str] | None) -> list[str]:
    if not phases:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in phases:
        if not isinstance(item, str):
            continue
        clean = safety.safe_str(item).strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
        if len(out) >= _MAX_TRIGGER_PHASES:
            break
    return out


def _clean_opt_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = safety.safe_str(value).strip()
    return clean or None


def _normalize_state(raw: object) -> dict[str, Any]:
    empty = _empty_state()
    if not isinstance(raw, Mapping):
        return empty
    if type(raw.get("version")) is not int or raw.get("version") != 1:
        return empty
    state = raw.get("state")
    if not isinstance(state, str) or state not in VALID_CATCHUP_STATES:
        return empty
    result = raw.get("result")
    if result is not None and (not isinstance(result, str) or result not in VALID_CATCHUP_RESULTS):
        result = None
    out: dict[str, Any] = {
        "version": 1,
        "state": state,
        "result": result,
        "started_at": _clean_opt_str(raw.get("started_at")),
        "finished_at": _clean_opt_str(raw.get("finished_at")),
        "trigger_phases": _normalize_phases(
            raw.get("trigger_phases") if isinstance(raw.get("trigger_phases"), list) else ()
        ),
        "event_file": _clean_opt_str(raw.get("event_file")),
        "event_sha256": _clean_opt_str(raw.get("event_sha256")),
        "detail": _clean_opt_str(raw.get("detail")),
    }
    retrigger_at = _clean_opt_str(raw.get("retrigger_at"))
    if retrigger_at is not None:
        out["retrigger_at"] = retrigger_at
    raw_retrigger_phases = raw.get("retrigger_phases")
    if isinstance(raw_retrigger_phases, list):
        retrigger_phases = _normalize_phases(raw_retrigger_phases)
        if retrigger_phases:
            out["retrigger_phases"] = retrigger_phases
    return out


def is_catchup_running() -> bool:
    """Return True if a background catch-up worker currently holds L1."""
    return lockfile.is_lock_held(CATCHUP_LOCK_PATH)


def read_catchup_state() -> dict[str, Any]:
    """Read-only snapshot of ``capture-catchup.json``. Never creates files."""
    try:
        with lockedjson.locked_json_snapshot(CATCHUP_STATE_PATH, blocking=False) as snap:
            if snap.state != "valid":
                return _empty_state()
            return _normalize_state(snap.data)
    except Exception:
        return _empty_state()


def record_catchup_state(
    *,
    state: str,
    result: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    trigger_phases: Sequence[str] = (),
    event_file: str | None = None,
    event_sha256: str | None = None,
    detail: str | None = None,
    consumed_retrigger_at: datetime | None = None,
) -> None:
    """Persist catch-up state atomically under L3. Best-effort."""
    clean_state = state if state in VALID_CATCHUP_STATES else "finished"
    clean_result = result if result in VALID_CATCHUP_RESULTS else None
    payload: dict[str, Any] = {
        "version": 1,
        "state": clean_state,
        "result": clean_result,
        "started_at": _clean_opt_str(started_at),
        "finished_at": _clean_opt_str(finished_at),
        "trigger_phases": _normalize_phases(trigger_phases),
        "event_file": _clean_opt_str(event_file),
        "event_sha256": _clean_opt_str(event_sha256),
        "detail": _clean_opt_str(detail),
    }
    try:
        with lockedjson.locked_json_rmw(
            CATCHUP_STATE_PATH,
            mode=0o600,
            compact=True,
            retry_intervals=_STATE_LOCK_BACKOFFS_S,
            on_contention="raise",
        ) as ljson:
            if ljson.read_state == "valid":
                existing = _normalize_state(ljson.data)
                retrigger_at = events.parse_aware_ts(existing.get("retrigger_at"))
                if retrigger_at is not None and (
                    consumed_retrigger_at is None or retrigger_at > consumed_retrigger_at
                ):
                    payload["retrigger_at"] = existing["retrigger_at"]
                    payload["retrigger_phases"] = existing.get("retrigger_phases", [])
            ljson.data.clear()
            ljson.data.update(payload)
    except Exception:
        pass


def _record_retrigger(phases: Sequence[str]) -> None:
    """Record a best-effort non-blocking re-check request while a worker holds L1."""
    clean_phases = _normalize_phases(phases)
    if not clean_phases:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with lockedjson.locked_json_rmw(
            CATCHUP_STATE_PATH,
            mode=0o600,
            compact=True,
            retry_intervals=(),
            on_contention="raise",
        ) as ljson:
            base = (
                _normalize_state(ljson.data)
                if ljson.read_state == "valid" and isinstance(ljson.data, Mapping)
                else _empty_state()
            )
            merged_phases = _normalize_phases([*base.get("retrigger_phases", []), *clean_phases])
            base["retrigger_at"] = now_iso
            base["retrigger_phases"] = merged_phases
            ljson.data.clear()
            ljson.data.update(base)
    except Exception:
        pass


def start_detached_catchup(trigger_phases: Sequence[str]) -> bool:
    """Spawn one detached catch-up worker, or record a retrigger if already running.

    Never waits, prompts, prints, or raises.
    """
    phases = _normalize_phases(trigger_phases)
    if not phases:
        return False
    try:
        try:
            lock_fd = lockfile.acquire_lock(CATCHUP_LOCK_PATH)
        except errors.LockError:
            if is_catchup_running():
                _record_retrigger(phases)
                return False
            # A status reader briefly probes with a shared lock. Try once
            # more if there was no exclusive owner to receive the trigger.
            lock_fd = lockfile.acquire_lock(CATCHUP_LOCK_PATH)
    except Exception:
        return False
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-P",
                "-m",
                "mind_meld.capturecatchup",
                f"--lock-fd={lock_fd}",
                *(f"--phase={phase}" for phase in phases),
            ],
            cwd=Path.cwd(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(lock_fd,),
        )
        return True
    except Exception:
        return False
    finally:
        lockfile.release_lock(CATCHUP_LOCK_PATH, unlock=False)


def catchup_observation() -> tuple[dict[str, Any], bool]:
    """Return ``(state, running)`` with crash/SIGKILL detection. Read-only."""
    state = read_catchup_state()
    running = is_catchup_running()
    if state["state"] == "running" and not running:
        # Recheck once to avoid racing a worker that just finished and released L1.
        state = read_catchup_state()
        running = is_catchup_running()
        if state["state"] == "running" and not running:
            state = {
                **state,
                "state": "finished",
                "result": "failed",
                "detail": "worker interrupted",
            }
    return state, running


def covers_deadline_backlog(
    state: Mapping[str, Any],
    *,
    running: bool,
    backlog_at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """Return True if a running or recent ``ok`` catch-up covers ``backlog_at``."""
    if running:
        return True
    if state.get("result") != "ok":
        return False
    finished_at = events.parse_aware_ts(state.get("finished_at"))
    if finished_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    if finished_at > current + _FUTURE_SKEW_WINDOW:
        return False
    if current - finished_at > CATCHUP_RECENT_WINDOW:
        return False
    if backlog_at is not None and finished_at < backlog_at:
        return False
    return True


def diag_catchup_state() -> dict[str, Any]:
    """Read-only non-secret catch-up state for ``mm diag``."""
    state, running = catchup_observation()
    return {
        "running": running,
        "state": state["state"],
        "result": state["result"],
        "started_at": state["started_at"],
        "finished_at": state["finished_at"],
        "trigger_phases": list(state["trigger_phases"]),
        "event_file": state["event_file"],
        "event_sha256": state["event_sha256"],
        "detail": state["detail"],
    }


def _config_fingerprint(cfg: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        cfg.get("device"),
        cfg.get("storage"),
        cfg.get("sync"),
        cfg.get("retro"),
    )


def _config_guard(cfg: Mapping[str, Any]) -> Callable[[], None]:
    expected = _config_fingerprint(cfg)

    def check_config_unchanged() -> None:
        if _config_fingerprint(config.load_config()) != expected:
            raise _ConfigChanged("capture configuration changed")

    return check_config_unchanged


def _acquire_sync_lock_bounded() -> bool:
    try:
        lockfile.acquire_lock(config.LOCK_PATH)
        return True
    except errors.LockError:
        pass
    for backoff in _SYNC_LOCK_BACKOFFS_S:
        time.sleep(backoff)
        try:
            lockfile.acquire_lock(config.LOCK_PATH)
            return True
        except errors.LockError:
            continue
    return False


def _select_step1_readers(
    all_readers: Sequence[tuple[str, events_tail.HostReader]],
    trigger_phases: Sequence[str],
    *,
    before_read: Callable[[], None],
) -> list[tuple[str, events_tail.HostReader]]:
    triggered_hosts = {
        phase.removeprefix("host:")
        for phase in trigger_phases
        if isinstance(phase, str) and phase.startswith("host:")
    }
    selected: list[tuple[str, events_tail.HostReader]] = []
    for name, reader_fn in all_readers:
        if "all" in triggered_hosts or name in triggered_hosts:
            selected.append((name, reader_fn))
            continue
        before_read()
        try:
            diag = host_registry.reader_usage_diag(name, count_logs=False)
            if host_registry.reader_cache_cold(name, diag):
                selected.append((name, reader_fn))
        except Exception:
            continue
    return selected


def _warm_step1_caches(
    trigger_phases: Sequence[str],
    timer: pushtimings.PhaseTimer,
) -> _WarmedCaches:
    """Warm consented private caches and collect any host deadline counts outside L2."""
    try:
        cfg = config.load_config()
    except Exception:
        return _WarmedCaches()
    sources = config.get_sources(cfg)
    mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if mm_events_src is None or not Path(mm_events_src["path"]).expanduser().is_dir():
        return _WarmedCaches(no_sources=True)

    device_id = str((cfg.get("device") or {}).get("id") or "")
    all_readers = events_tail.consented_host_readers(cfg, sources)
    check_config_unchanged = _config_guard(cfg)
    try:
        warm_readers = _select_step1_readers(
            all_readers, trigger_phases, before_read=check_config_unchanged
        )
    except _ConfigChanged:
        return _WarmedCaches()
    warm_reader_names = frozenset(name for name, _ in warm_readers)
    step1_timed_hosts: set[str] = set()
    deadline_evidence: Mapping[str, events_tail.HostReadEvidence] = {}

    if warm_readers:
        try:
            host_budget_ms = events_tail.resolve_host_read_budget(cfg, interactive=True)[0]
            with events_tail.phase_timer_scope(timer):
                host_capture, _ = events_tail._capture_host_snapshot(
                    device_id,
                    warm_readers,
                    host_budget_ms=host_budget_ms,
                    warm_host_cache=events_tail._warm_host_cache_with_notice,
                    before_read=check_config_unchanged,
                )
            step1_timed_hosts.update(warm_reader_names)
            deadline_evidence = host_capture.deadline_evidence
        except Exception:
            pass

    claude_paths = events_tail._enabled_claude_paths(sources)
    if claude_paths and token_usage.is_cache_cold():
        try:
            total_ms, _ = events_tail.resolve_attended_capture_cap(cfg)
            warm_budget_s = max(
                1.0,
                min(
                    token_usage.DEFAULT_WARM_BUDGET_S,
                    (total_ms - events_tail._host_phase_ms(total_ms)) / 1000.0,
                ),
            )
            token_usage.warm_token_cache_inline(
                claude_paths,
                deadline_s=warm_budget_s,
                before_read=check_config_unchanged,
            )
        except Exception:
            pass

    return _WarmedCaches(
        timed_hosts=frozenset(step1_timed_hosts),
        config_stamp=_config_fingerprint(cfg),
        deadline_evidence=deadline_evidence,
    )


def _attended_tail_budgets(
    cfg: dict[str, Any],
    *,
    since: datetime,
) -> tuple[int, int, int, float | None, int | None, int]:
    """Compute attended scan-cap budgets matching ``events_tail._run_events_tail``."""
    retro = cfg.get("retro")
    legacy_only = (
        isinstance(retro, dict)
        and "host_usage_interactive_budget_ms" in retro
        and "capture_budget_ms" not in retro
        and "attended_capture_cap_ms" not in retro
    )
    if legacy_only:
        budget_ms = events.WALK_TIME_BUDGET_INTERACTIVE_MS
        git_budget_ms = events.git_walk_budget_ms(quiet=False, since=since)
        root_discovery_budget_ms = events.ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS
        tail_deadline: float | None = None
        per_repo_cap_ms: int | None = None
    else:
        capture_ms, _ = events_tail.resolve_capture_budget(cfg)
        total_ms, _ = events_tail.resolve_attended_capture_cap(cfg)
        extension_ms = max(0, total_ms - capture_ms)
        budget_ms = max(1, total_ms - events_tail._host_phase_ms(total_ms))
        git_budget_ms = min(
            events.git_walk_budget_ms(quiet=False, since=since) + extension_ms,
            budget_ms,
        )
        root_discovery_budget_ms = events.ROOT_DISCOVERY_BUDGET_ATTENDED_MS + extension_ms
        tail_deadline = time.monotonic() + budget_ms / 1000.0
        per_repo_cap_ms = events.PER_REPO_TIMEOUT_CAP_MS + extension_ms
    host_budget_ms = events_tail.resolve_host_read_budget(cfg, interactive=True)[0]
    return (
        budget_ms,
        git_budget_ms,
        root_discovery_budget_ms,
        tail_deadline,
        per_repo_cap_ms,
        host_budget_ms,
    )


def _classify_capture_outcome(
    capture: events_tail.CaptureResult,
    *,
    claude_paths: Sequence[Path],
    append_skipped: bool,
    git_confirmed: bool,
) -> tuple[str, str | None]:
    if append_skipped:
        return "failed", "event append skipped (max-file-size)"
    if not git_confirmed:
        return "failed", "Git snapshot or cursor update could not be confirmed; run mm diag"
    if capture.walk_errors > 0:
        return "failed", f"git walk had {capture.walk_errors} error(s)"
    if any(err != events.GIT_ROOT_DISCOVERY_BUDGET_ERROR for err in capture.root_discovery.errors):
        return "failed", "git root discovery failed"
    if events.classify_discovery(capture.root_discovery) == "empty":
        return "failed", "no git repositories discovered"
    if (claude_paths and not capture.token_cache_requested) or capture.warn_lock_unavailable:
        return "failed", "Claude token cache unavailable"
    if any(reason != "deadline" for _, reason in capture.host_capture.dropped) or (
        not capture.host_capture.complete
        and not capture.host_capture.dropped
        and capture.host_capture.reason != "deadline"
    ):
        return "failed", "host usage reader failed"
    if events_tail.deadline_stopped_phases(capture):
        return "partial", "capture still stopped at deadline; run mm push"
    return "ok", None


def _run_catchup_pass(
    trigger_phases: Sequence[str],
    timer: pushtimings.PhaseTimer,
) -> dict[str, Any]:
    warmed = _warm_step1_caches(trigger_phases, timer)
    step2_started_at = datetime.now(timezone.utc)

    def finish(
        result: str,
        detail: str | None = None,
        *,
        event_file: str | None = None,
        event_sha256: str | None = None,
    ) -> dict[str, Any]:
        return {
            "result": result,
            "detail": detail,
            "event_file": event_file,
            "event_sha256": event_sha256,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "step2_started_at": step2_started_at,
        }

    if warmed.no_sources:
        return finish("no-sources")
    if not _acquire_sync_lock_bounded():
        return finish("deferred", "sync lock busy; run mm push")

    try:
        try:
            cfg = config.load_config()
        except Exception as exc:
            return finish("failed", f"configuration unreadable ({type(exc).__name__})")

        check_config_unchanged = _config_guard(cfg)
        try:
            resolved = config.resolve_sources(cfg, strict=True, bootstrap=False)
        except Exception as exc:
            return finish("failed", f"source configuration invalid ({type(exc).__name__})")

        sources = resolved.available
        mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
        if mm_events_src is None or not Path(mm_events_src["path"]).expanduser().is_dir():
            return finish("no-sources")

        # Rule out PR #382 F42/F41: reject pre-existing empty/corrupt/unreadable
        # git-capture-cache.json while allowing absent ("missing") first use.
        try:
            with lockedjson.locked_json_snapshot(
                config.GIT_CAPTURE_CACHE_PATH, blocking=False
            ) as snap:
                cache_state = snap.state
        except OSError:
            cache_state = "unreadable"
        if cache_state not in {"valid", "missing"}:
            return finish("failed", "Git capture index unreadable; run mm diag")

        device_id = str((cfg.get("device") or {}).get("id") or "")
        events_dir = Path(mm_events_src["path"]).expanduser() / "events"
        events.reconcile_uncommitted_push_cursors(events_dir, device_id)
        cursor = events.resolve_push_cursor(events_dir, device_id)
        since = events.effective_capture_since([], cursor.since)

        (
            budget_ms,
            git_budget_ms,
            root_discovery_budget_ms,
            tail_deadline,
            per_repo_cap_ms,
            host_budget_ms,
        ) = _attended_tail_budgets(cfg, since=since)

        claude_paths = events_tail._enabled_claude_paths(sources)
        host_readers = events_tail.consented_host_readers(cfg, sources)
        warm_deadline_evidence = (
            warmed.deadline_evidence if warmed.config_stamp == _config_fingerprint(cfg) else None
        )

        def prepare_token_cache_warm_only() -> events_tail.CacheLockMode:
            if not claude_paths:
                return None
            if token_usage.is_cache_cold():
                return None
            return "warn"

        with events_tail.phase_timer_scope(timer):
            capture = events_tail._capture_event_snapshots(
                cfg,
                claude_paths,
                device_id,
                since=since,
                budget_ms=budget_ms,
                git_budget_ms=git_budget_ms,
                persist=True,
                use_repo_cursors=True,
                root_discovery_budget_ms=root_discovery_budget_ms,
                host_budget_ms=host_budget_ms,
                session_deadline=tail_deadline,
                per_repo_cap_ms=per_repo_cap_ms,
                warm_host_cache=None,
                origin=events.GIT_SNAPSHOT_ORIGIN_CATCHUP,
                prepare_token_cache=prepare_token_cache_warm_only,
                host_readers=host_readers,
                before_read=check_config_unchanged,
                preserve_budget_for=warmed.timed_hosts,
                warm_deadline_evidence=warm_deadline_evidence,
            )
        check_config_unchanged()

        if capture.git_rows:
            since = datetime.fromisoformat(capture.git_rows[0]["git_capture"]["since"])

        now_utc = datetime.now(timezone.utc)
        published_git_left: int | None = None
        if capture.walk_budget_aborts > 0 and capture.walk_errors == 0:
            try:
                preview_left = events.preview_pending_repositories(
                    capture.root_discovery.roots,
                    capture.git_rows,
                    since=since,
                    cursor_ts=now_utc,
                )
            except Exception:
                preview_left = None
            if type(preview_left) is int and 1 <= preview_left <= events.MAX_PENDING_LEFT:
                published_git_left = preview_left
                for row in capture.git_rows:
                    row_capture = row.get("git_capture")
                    if isinstance(row_capture, dict):
                        row_capture["pending_left"] = preview_left

        rows = [*capture.git_rows, *capture.session_rows, *capture.host_rows]
        append_result: Path | events.EventAppendSkipped | None = None
        append_skipped = False
        if rows:
            check_config_unchanged()
            append_result = events.write_push_event(
                events_dir,
                device_id,
                rows,
                strict=True,
                max_file_size=cfg.get("sync", {}).get(
                    "max_file_size", config.DEFAULT_MAX_FILE_SIZE
                ),
            )
            append_skipped = isinstance(append_result, events.EventAppendSkipped)

        complete_git = (
            events.classify_discovery(capture.root_discovery) not in {"partial", "empty"}
            and not capture.walk_budget_aborts
            and not capture.walk_errors
        )
        if append_skipped:
            git_confirmed = False
        else:
            git_confirmed = events_tail._commit_recorded_git_rows(
                events_dir,
                device_id,
                capture.git_rows,
                since=since,
                complete=complete_git,
            )
            if git_confirmed and (complete_git or published_git_left is not None):
                events_tail._record_git_progress(
                    device_id=device_id,
                    published=published_git_left,
                    clear=published_git_left is None,
                    now=now_utc,
                )

        event_file: str | None = None
        event_sha256: str | None = None
        if isinstance(append_result, Path) and append_result.is_file():
            event_file = f"events/{append_result.name}"
            try:
                event_sha256 = manifest.hash_file(append_result)
            except OSError:
                event_sha256 = None

        outcome, detail = _classify_capture_outcome(
            capture,
            claude_paths=claude_paths,
            append_skipped=append_skipped,
            git_confirmed=git_confirmed,
        )
        # Rule out PR #382 F25: stamp finished_at BEFORE releasing L2 so any
        # subsequent autopush acquiring L2 gets auto_push_at > finished_at.
        return finish(outcome, detail, event_file=event_file, event_sha256=event_sha256)
    except _ConfigChanged:
        return finish("deferred", "capture configuration changed; run mm push")
    except Exception as exc:
        return finish("failed", f"catch-up failed ({type(exc).__name__})")
    finally:
        lockfile.release_lock(config.LOCK_PATH)


def run_catchup(
    trigger_phases: Sequence[str],
    *,
    inherited_lock_fd: int | None = None,
) -> dict[str, Any] | None:
    """Run single-flight background catch-up under L1.

    Returns the final pass result dict, or ``None`` if another worker already
    holds L1.
    """
    phases = _normalize_phases(trigger_phases)
    try:
        if inherited_lock_fd is None:
            lockfile.acquire_lock(CATCHUP_LOCK_PATH)
        else:
            lockfile.adopt_lock(CATCHUP_LOCK_PATH, inherited_lock_fd)
    except errors.LockError:
        return None

    started_at = datetime.now(timezone.utc).isoformat()
    previous = read_catchup_state()
    consumed_retrigger_at = events.parse_aware_ts(previous.get("retrigger_at"))
    phases = _normalize_phases([*phases, *previous.get("retrigger_phases", [])])
    timer = pushtimings.PhaseTimer()
    final_res: dict[str, Any] = {
        "result": "failed",
        "detail": "unexpected catch-up error",
        "event_file": None,
        "event_sha256": None,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        record_catchup_state(
            state="running",
            result=None,
            started_at=started_at,
            finished_at=None,
            trigger_phases=phases,
            event_file=None,
            event_sha256=None,
            detail=None,
            consumed_retrigger_at=consumed_retrigger_at,
        )
        pass_res = _run_catchup_pass(phases, timer)
        final_res = pass_res

        # Bounded at-most-one follow-up pass if a concurrent hook recorded a
        # retrigger newer than Step 2's start (rules out PR #382 F28).
        state_now = read_catchup_state()
        retrigger_at = events.parse_aware_ts(state_now.get("retrigger_at"))
        step2_started = pass_res.get("step2_started_at")
        if (
            retrigger_at is not None
            and isinstance(step2_started, datetime)
            and retrigger_at > step2_started
        ):
            consumed_retrigger_at = retrigger_at
            phases = _normalize_phases(state_now.get("retrigger_phases") or phases)
            record_catchup_state(
                state="running",
                result=None,
                started_at=started_at,
                finished_at=None,
                trigger_phases=phases,
                event_file=None,
                event_sha256=None,
                detail=None,
                consumed_retrigger_at=consumed_retrigger_at,
            )
            final_res = _run_catchup_pass(phases, timer)
    except Exception as exc:
        final_res = {
            "result": "failed",
            "detail": f"catch-up failed ({type(exc).__name__})",
            "event_file": None,
            "event_sha256": None,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        try:
            if recorded_phases := timer.phases():
                pushtimings.record_push_timings(
                    "catchup",
                    recorded_phases,
                    total_ms=timer.total_ms(),
                )
        finally:
            try:
                record_catchup_state(
                    state="finished",
                    result=final_res["result"],
                    started_at=started_at,
                    finished_at=final_res["finished_at"],
                    trigger_phases=phases,
                    event_file=final_res["event_file"],
                    event_sha256=final_res["event_sha256"],
                    detail=final_res["detail"],
                    consumed_retrigger_at=consumed_retrigger_at,
                )
            finally:
                # The detached child keeps custody through interpreter exit,
                # including Git work still unwinding at the capture deadline.
                if inherited_lock_fd is None:
                    lockfile.release_lock(CATCHUP_LOCK_PATH)
    return final_res


def _parse_argv(argv: Sequence[str] | None = None) -> list[str]:
    args = list(sys.argv[1:] if argv is None else argv)
    phases: list[str] = []
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg.startswith("--phase="):
            phases.append(arg.removeprefix("--phase="))
        elif arg == "--phase" and idx + 1 < len(args):
            phases.append(args[idx + 1])
            idx += 1
        idx += 1
    return _normalize_phases(phases)


def _detached_main(argv: Sequence[str] | None = None) -> int:
    """Silent detached worker entrypoint."""
    args = sys.argv[1:] if argv is None else argv
    phases = _parse_argv(args)
    if not phases:
        return 0
    with (
        redirect_stdout(StringIO()),
        redirect_stderr(StringIO()),
        token_usage.diagnostic_scope(lambda *a, **k: None),
    ):
        try:
            inherited_fd = next(
                (
                    int(arg.removeprefix("--lock-fd="))
                    for arg in args
                    if arg.startswith("--lock-fd=")
                ),
                None,
            )
            run_catchup(phases, inherited_lock_fd=inherited_fd)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(_detached_main())
