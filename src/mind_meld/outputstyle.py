"""Pure line constructors for the shared CLI output style."""

from __future__ import annotations

import re


def _error_line(message: str) -> str:
    return f"mm: error: {message}"


def _fix_line(command: str) -> str:
    return f"Run: {command}"


def _warning_line(message: str, *, fix: str | None = None) -> str:
    line = f"mm: warning: {message}"
    if fix is not None:
        return f"{line} {_fix_line(fix)}"
    return line


def _notice_line(message: str, *, fix: str | None = None) -> str:
    line = f"mm: notice: {message}"
    if fix is not None:
        return f"{line} {_fix_line(fix)}"
    return line


def _outcome_line(verb: str, text: str) -> str:
    return f"{verb.title()}: {text}."


def _fix_for(message: str, fallback: str | None = None) -> tuple[str, str | None]:
    command: str | None
    if "newer mm" in message or "unsupported format" in message:
        command = "mm update"
    elif "mm migrate-config" in message:
        command = "mm migrate-config"
    elif "mm init" in message:
        command = "mm init"
    elif message.startswith("Device not found"):
        command = "mm devices"
    else:
        command = fallback
    problem = message
    if command is not None:
        escaped = re.escape(command)
        stripped = re.sub(
            rf"(?:,? then (?:run |retry )?|(?:(?:\s*—\s*|\.\s+))(?:run|retry) )"
            rf"[`']?{escaped}[`']?(?:\s+first)?\.?$",
            "",
            problem,
            flags=re.IGNORECASE,
        )
        if stripped != problem:
            problem = stripped.rstrip(". —")
    return problem, command
