"""Read-only Grok skill-discovery probe for ``mm diag``.

Not a ``skill_link`` registry. Grok 1.0.5 already loads ``retro-fleet`` from
``~/.claude/skills`` via default-on Claude compatibility, so mm does not
maintain a Grok ``AgentRow``. This probe answers a different question: can
the host actually load the skill? ``mm diag`` is the only caller — never
status, push, or autopush.

``GROK_HOME`` is a ``host_usage`` sessions-only override
(``host_usage.py``) and ``tests/conftest.py`` deletes it. A read-only probe
publishes nothing, so the ROADMAP "no shared resolver" rule does not bind
here.

Do not reuse ``config.grok_customization_dirs_exist`` as a presence probe.
That predicate is True only when ``skills/``, ``commands/``, or ``rules/``
exist under ``~/.grok``. Grok is installed and loading the skill on machines
where those dirs are absent (this Mac, 2026-08-24). Presence for this probe
is ``shutil.which("grok")``.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import time
from typing import Any

from mind_meld.safety import safe_str

PROBE_TIMEOUT_S = 2.0
_STDOUT_CAP = 256 * 1024  # 116KB today is not a future contract
_RENDER_CAP = 200
_SKILL_NAME = "retro-fleet"

STATUS_OK = "ok"
STATUS_BINARY_ABSENT = "binary-absent"
STATUS_TIMEOUT = "timeout"
STATUS_NONZERO_EXIT = "nonzero-exit"
STATUS_MALFORMED_JSON = "malformed-json"
STATUS_UNSUPPORTED_SCHEMA = "unsupported-schema"

_FAILURE_STATUSES = (
    STATUS_BINARY_ABSENT,
    STATUS_TIMEOUT,
    STATUS_NONZERO_EXIT,
    STATUS_MALFORMED_JSON,
    STATUS_UNSUPPORTED_SCHEMA,
)


def _cap(value: str) -> str:
    text = safe_str(value)
    if len(text) > _RENDER_CAP:
        return text[:_RENDER_CAP]
    return text


def _failure(status: str) -> dict[str, Any]:
    return {"host": "grok", "status": status}


def _claude_skills_compat(payload: dict[str, Any]) -> bool | None:
    compat = payload.get("externalCompat")
    if not isinstance(compat, dict):
        return None
    cells = compat.get("cells")
    if not isinstance(cells, list):
        return None
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        if cell.get("vendor") == "claude" and cell.get("surface") == "skills":
            enabled = cell.get("enabled")
            return enabled if isinstance(enabled, bool) else None
    return None


def _retro_fleet(payload: dict[str, Any]) -> tuple[bool, str | None] | None:
    skills = payload.get("skills")
    if not isinstance(skills, list):
        return None
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        if skill.get("name") != _SKILL_NAME:
            continue
        source = skill.get("source")
        path: str | None = None
        if isinstance(source, dict):
            raw_path = source.get("path")
            if isinstance(raw_path, str) and raw_path:
                path = raw_path
        return True, path
    return False, None


def _kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        proc.kill()
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _run_inspect(grok: str) -> tuple[str | None, bytes]:
    """Run ``grok inspect --json``. Status string on failure, else (None, stdout)."""
    try:
        proc = subprocess.Popen(
            [grok, "inspect", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            bufsize=0,
        )
    except OSError:
        return STATUS_BINARY_ABSENT, b""

    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    chunks: list[bytes] = []
    n = 0
    capped = False
    deadline = time.monotonic() + PROBE_TIMEOUT_S
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill(proc)
                return STATUS_TIMEOUT, b""
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            piece = os.read(fd, 65536)
            if not piece:
                break
            space = _STDOUT_CAP - n
            if space <= 0:
                capped = True
                _kill(proc)
                break
            if len(piece) > space:
                chunks.append(piece[:space])
                n += space
                capped = True
                _kill(proc)
                break
            chunks.append(piece)
            n += len(piece)
        if not capped:
            leftover = deadline - time.monotonic()
            if leftover <= 0:
                _kill(proc)
                return STATUS_TIMEOUT, b""
            try:
                proc.wait(timeout=leftover)
            except subprocess.TimeoutExpired:
                _kill(proc)
                return STATUS_TIMEOUT, b""
    except OSError:
        _kill(proc)
        return STATUS_BINARY_ABSENT, b""
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass

    raw = b"".join(chunks)
    if capped:
        return STATUS_MALFORMED_JSON, b""
    if proc.returncode is None or proc.returncode != 0:
        return STATUS_NONZERO_EXIT, b""
    return None, raw


def probe_grok_skill_discovery() -> dict[str, Any]:
    """Return a diag-only snapshot of whether Grok can load ``retro-fleet``.

    Never persists, never syncs, never interpolates raw stdout/stderr,
    unknown fields, or parse exceptions into the result.
    """
    grok = shutil.which("grok")
    if grok is None:
        return _failure(STATUS_BINARY_ABSENT)

    status, raw = _run_inspect(grok)
    if status is not None:
        return _failure(status)

    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return _failure(STATUS_MALFORMED_JSON)

    if not isinstance(payload, dict):
        return _failure(STATUS_UNSUPPORTED_SCHEMA)

    version = payload.get("grokVersion")
    if not isinstance(version, str) or not version:
        return _failure(STATUS_UNSUPPORTED_SCHEMA)

    compat = _claude_skills_compat(payload)
    if compat is None:
        return _failure(STATUS_UNSUPPORTED_SCHEMA)

    fleet = _retro_fleet(payload)
    if fleet is None:
        return _failure(STATUS_UNSUPPORTED_SCHEMA)
    resolved, path = fleet

    row: dict[str, Any] = {
        "host": "grok",
        "status": STATUS_OK,
        "claude_skills_compat": compat,
        "retro_fleet_resolved": resolved,
        "grok_version": _cap(version),
    }
    if path is not None:
        row["retro_fleet_path"] = _cap(path)
    else:
        row["retro_fleet_path"] = None
    return row
