"""Muse sync source: hardcoded memory/ walker + config activation.

The walker tests mirror the Grok boundary tests in test_manifest.py:
sessions, credentials and generator trees at the same root must upload
zero files, including when reached through a symlink or a hard link.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mind_meld.config import (
    ConfigError,
    _validate_sources,
    get_default_source,
    muse_memory_dir_exists,
)
from mind_meld.manifest import walk_muse_source, walk_source


def _mixed_root(tmp_path: Path) -> Path:
    root = tmp_path / "muse"
    personal = root / "memory" / "personal"
    personal.mkdir(parents=True)
    (personal / "MEMORY.md").write_text("# index\n")
    project = root / "memory" / "projects" / "Users-kb-app-0c84"
    project.mkdir(parents=True)
    (project / "notes.md").write_text("project note\n")
    sessions = root / "sessions" / "2026" / "10" / "13" / "s1"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text('{"prompt": "secret"}\n')
    (root / "session-index.db").write_bytes(b"db")
    bundled = root / "skills" / "bundled" / "muse-core"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("generated\n")
    (root / "tui-history.jsonl").write_text("{}\n")
    return root


def test_walker_syncs_only_memory_tree(tmp_path):
    root = _mixed_root(tmp_path)
    src = get_default_source("muse")
    assert src is not None
    src["path"] = str(root)
    files = walk_muse_source(src)
    assert set(files) == {
        "memory/personal/MEMORY.md",
        "memory/projects/Users-kb-app-0c84/notes.md",
    }


def test_muse_type_dispatches_to_walk_muse_source(tmp_path):
    root = _mixed_root(tmp_path)
    _, files = walk_source({"name": "muse", "path": str(root), "type": "muse"})
    assert "memory/personal/MEMORY.md" in files
    assert not any(rel.startswith("sessions/") for rel in files)


def test_nested_symlink_into_sessions_is_not_walked(tmp_path):
    root = tmp_path / "muse"
    sessions = root / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text("prompt\n")
    memory = root / "memory"
    memory.mkdir()
    (memory / "evil").symlink_to(sessions, target_is_directory=True)
    files = walk_muse_source({"path": str(root), "type": "muse"})
    assert files == {}


def test_hardlink_to_session_log_is_not_walked(tmp_path):
    root = tmp_path / "muse"
    sessions = root / "sessions"
    sessions.mkdir(parents=True)
    target = sessions / "session.jsonl"
    target.write_text("secret")
    memory = root / "memory"
    memory.mkdir()
    (memory / "copied-secret").hardlink_to(target)
    skipped: list[tuple[str, str]] = []
    files = walk_muse_source(
        {"path": str(root), "type": "muse"},
        on_skip=lambda path, reason: skipped.append((path, reason)),
    )
    assert files == {}
    assert skipped == [(str(memory / "copied-secret"), "hardlink")]


def test_muse_memory_lock_never_roams(tmp_path):
    """Muse's machine-local PID lock stays local on push and on pull."""
    from mind_meld.cli import _build_exclude_map, _filter_excluded_paths

    root = tmp_path / "muse"
    personal = root / "memory" / "personal"
    personal.mkdir(parents=True)
    (personal / ".muse-memory.lock").write_text("pid=30489\n")
    (personal / "MEMORY.md").write_text("# index\n")
    src = {"name": "muse", "path": str(root), "type": "muse"}
    assert set(walk_muse_source(src)) == {"memory/personal/MEMORY.md"}

    exclude_map, prefixes = _build_exclude_map({}, [src])
    incoming = {
        "sources": {
            "muse": {
                "files": {
                    "memory/personal/MEMORY.md": {"sha256": "a" * 64},
                    "memory/personal/.muse-memory.lock": {"sha256": "b" * 64},
                }
            }
        },
        "tombstones": {},
    }
    filtered = _filter_excluded_paths(incoming, exclude_map, prefixes)
    assert set(filtered["sources"]["muse"]["files"]) == {"memory/personal/MEMORY.md"}


def test_missing_memory_dir_is_a_noop(tmp_path):
    root = tmp_path / "muse"
    root.mkdir()
    assert walk_muse_source({"path": str(root), "type": "muse"}) == {}
    assert not muse_memory_dir_exists(root)


def test_default_source_never_auto_activates(monkeypatch, tmp_path):
    """Muse creates memory/ itself, so no tree is consent: explicit enable only."""
    from mind_meld import config as config_module

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".local" / "share" / "muse" / "memory" / "personal").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert muse_memory_dir_exists(home / ".local" / "share" / "muse")

    # Selection, not availability: both legacy shapes (no sync table, and
    # claude_dir with auto-detect) must leave muse out.
    for cfg in ({}, {"sync": {"claude_dir": str(home / ".claude")}}):
        selected = config_module.resolve_sources(cfg).selected
        assert "muse" not in [s["name"] for s in selected], cfg

    explicit = {"sync": {"sources": [get_default_source("muse")]}}
    assert [s["name"] for s in config_module.resolve_sources(explicit).selected] == ["muse"]


def test_muse_name_and_type_are_reserved_for_each_other():
    with pytest.raises(ConfigError, match="type 'muse' is reserved"):
        _validate_sources([{"name": "mine", "path": "~/x", "type": "muse"}])
    with pytest.raises(ConfigError, match="source 'muse' must use type 'muse'"):
        _validate_sources([{"name": "muse", "path": "~/x", "type": "generic"}])
    with pytest.raises(ConfigError, match="does not support include_dirs"):
        _validate_sources(
            [{"name": "muse", "path": "~/x", "type": "muse", "include_dirs": ["sessions"]}]
        )
