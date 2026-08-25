"""`mm diag` Grok skill-discovery probe. Never status, push, or autopush."""

from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path

from mind_meld import host_skill_discovery as hsd


def _install_fake_grok(tmp_path: Path, monkeypatch, body: str, *, name: str = "grok") -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), "/bin", "/usr/bin"]))
    return path


def _ok_payload(**overrides) -> dict:
    payload = {
        "grokVersion": "1.0.5",
        "skills": [
            {
                "name": "retro-fleet",
                "source": {"type": "user", "path": "/Users/kb/.claude/skills/retro-fleet/SKILL.md"},
            }
        ],
        "externalCompat": {
            "cells": [
                {"vendor": "claude", "surface": "skills", "enabled": True, "source": "default"},
            ]
        },
    }
    payload.update(overrides)
    return payload


def test_binary_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    row = hsd.probe_grok_skill_discovery()
    assert row == {"host": "grok", "status": hsd.STATUS_BINARY_ABSENT}


def test_timeout(tmp_path, monkeypatch):
    _install_fake_grok(tmp_path, monkeypatch, "sleep 30\n")
    monkeypatch.setattr(hsd, "PROBE_TIMEOUT_S", 0.05)
    row = hsd.probe_grok_skill_discovery()
    assert row == {"host": "grok", "status": hsd.STATUS_TIMEOUT}


def test_launch_failure_is_binary_absent(monkeypatch):
    monkeypatch.setattr(hsd.shutil, "which", lambda _: "grok")

    def raise_oserror(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(hsd.subprocess, "Popen", raise_oserror)
    row = hsd.probe_grok_skill_discovery()
    assert row == {"host": "grok", "status": hsd.STATUS_BINARY_ABSENT}


def test_nonzero_exit(tmp_path, monkeypatch):
    _install_fake_grok(tmp_path, monkeypatch, "echo nope >&2\nexit 2\n")
    row = hsd.probe_grok_skill_discovery()
    assert row == {"host": "grok", "status": hsd.STATUS_NONZERO_EXIT}
    assert "nope" not in json.dumps(row)


def test_malformed_json(tmp_path, monkeypatch):
    _install_fake_grok(tmp_path, monkeypatch, "printf 'not-json'\n")
    row = hsd.probe_grok_skill_discovery()
    assert row == {"host": "grok", "status": hsd.STATUS_MALFORMED_JSON}
    assert "not-json" not in json.dumps(row)


def test_malformed_json_handles_parser_depth_error(monkeypatch):
    monkeypatch.setattr(hsd.shutil, "which", lambda _: "grok")
    monkeypatch.setattr(hsd, "_run_inspect", lambda _: (None, b"[]"))

    def raise_recursion_error(_):
        raise RecursionError("nested JSON")

    monkeypatch.setattr(hsd.json, "loads", raise_recursion_error)
    row = hsd.probe_grok_skill_discovery()
    assert row == {"host": "grok", "status": hsd.STATUS_MALFORMED_JSON}


def test_stdout_cap_is_malformed_json(tmp_path, monkeypatch):
    _install_fake_grok(tmp_path, monkeypatch, f"head -c {hsd._STDOUT_CAP + 1} /dev/zero\n")
    row = hsd.probe_grok_skill_discovery()
    assert row == {"host": "grok", "status": hsd.STATUS_MALFORMED_JSON}


def test_unsupported_schema(tmp_path, monkeypatch):
    _install_fake_grok(tmp_path, monkeypatch, "printf '{}'\n")
    row = hsd.probe_grok_skill_discovery()
    assert row == {"host": "grok", "status": hsd.STATUS_UNSUPPORTED_SCHEMA}


def test_ok_extracts_four_values_only(tmp_path, monkeypatch):
    payload = _ok_payload()
    payload["secrets"] = "should-not-leak"
    out = tmp_path / "inspect.json"
    out.write_text(json.dumps(payload))
    _install_fake_grok(tmp_path, monkeypatch, f"cat {out}\n")
    row = hsd.probe_grok_skill_discovery()
    assert row["status"] == hsd.STATUS_OK
    assert row["host"] == "grok"
    assert row["claude_skills_compat"] is True
    assert row["retro_fleet_resolved"] is True
    assert row["retro_fleet_path"] == "/Users/kb/.claude/skills/retro-fleet/SKILL.md"
    assert row["grok_version"] == "1.0.5"
    assert "secrets" not in row
    assert set(row) == {
        "host",
        "status",
        "claude_skills_compat",
        "retro_fleet_resolved",
        "retro_fleet_path",
        "grok_version",
    }


def test_ok_when_retro_fleet_is_absent(tmp_path, monkeypatch):
    payload = _ok_payload(skills=[{"name": "other"}])
    out = tmp_path / "inspect.json"
    out.write_text(json.dumps(payload))
    _install_fake_grok(tmp_path, monkeypatch, f"cat {out}\n")
    row = hsd.probe_grok_skill_discovery()
    assert row["status"] == hsd.STATUS_OK
    assert row["retro_fleet_resolved"] is False
    assert row["retro_fleet_path"] is None


def test_does_not_use_grok_home(tmp_path, monkeypatch):
    """GROK_HOME is a host_usage sessions override; the probe must ignore it."""
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "not-a-grok-home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    row = hsd.probe_grok_skill_discovery()
    assert row["status"] == hsd.STATUS_BINARY_ABSENT


def test_failure_states_are_five_distinct_strings():
    assert len(set(hsd._FAILURE_STATUSES)) == 5
    assert hsd.STATUS_OK not in hsd._FAILURE_STATUSES


def test_probe_is_called_only_from_collect_diag_state():
    """mm diag only — never status, push, or autopush."""
    src = Path(__file__).resolve().parents[1] / "src" / "mind_meld" / "cli.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    calls: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.func = "<module>"

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            prev = self.func
            self.func = node.name
            self.generic_visit(node)
            self.func = prev

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name = None
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name == "probe_grok_skill_discovery":
                calls.append(self.func)
            self.generic_visit(node)

    Visitor().visit(tree)
    assert calls == ["_collect_diag_state"]
