"""Tests for Track 159A: local qmd memory index, sync tails, status, and diag."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from mind_meld import (
    cli,
    config,
    core,
    crypto,
    devices,
    memoryindex,
    pullflow,
    sidecar,
    statedb,
    statusflow,
)
from mind_meld.storage import local as local_storage

PASSPHRASE = "memory-index-test-passphrase"
MEMORY_KB = 8192
runner = CliRunner()

_QMD_STUB_SOURCE = textwrap.dedent(
    """\
    import fnmatch
    import json
    import os
    import re
    import sys
    import time
    from pathlib import Path

    STATE_PATH = Path(os.environ["QMD_STUB_STATE"])
    LOG_PATH = Path(os.environ["QMD_STUB_LOG"])
    VERSION = os.environ.get("QMD_STUB_VERSION", "qmd 2.8.3")
    FAIL_CMD = os.environ.get("QMD_STUB_FAIL", "")
    SLEEP_S = float(os.environ.get("QMD_STUB_SLEEP", "0") or "0")


    def log_call(argv):
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\\n")


    def load_state():
        if not STATE_PATH.exists():
            return {"collections": {}, "docs": {}}
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))


    def save_state(state):
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


    def expand_braces(pattern):
        m = re.search(r"\\{([^{}]+)\\}", pattern)
        if not m:
            return [pattern]
        prefix = pattern[: m.start()]
        suffix = pattern[m.end() :]
        out = []
        for part in m.group(1).split(","):
            out.extend(expand_braces(prefix + part + suffix))
        return out


    def match_glob(rel_posix, pattern):
        for pat in expand_braces(pattern):
            # Support ** matching zero or more path segments.
            regex_parts = []
            i = 0
            while i < len(pat):
                if pat[i : i + 3] == "**/":
                    regex_parts.append("(?:.*/)?")
                    i += 3
                elif pat[i : i + 2] == "**":
                    regex_parts.append(".*")
                    i += 2
                elif pat[i] == "*":
                    regex_parts.append("[^/]*")
                    i += 1
                elif pat[i] == "?":
                    regex_parts.append("[^/]")
                    i += 1
                else:
                    regex_parts.append(re.escape(pat[i]))
                    i += 1
            if re.fullmatch("".join(regex_parts), rel_posix):
                return True
        return False


    def scan_collection(col_path, mask):
        root = Path(col_path)
        docs = {}
        if not root.is_dir():
            return docs
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                rel = p.relative_to(root).as_posix()
                if match_glob(rel, mask):
                    docs[rel] = p.read_text(encoding="utf-8", errors="replace")
        return docs


    def main():
        argv = sys.argv[1:]
        if argv[:2] == ["--index", "index"]:
            argv = argv[2:]
        log_call(argv)
        if SLEEP_S > 0:
            time.sleep(SLEEP_S)
        if not argv:
            sys.exit(1)

        if argv[0] == "--version":
            if FAIL_CMD == "version":
                sys.exit(1)
            print(VERSION)
            return

        if argv[0] == "collection":
            if len(argv) < 2:
                sys.exit(1)
            sub = argv[1]
            if FAIL_CMD == f"collection {sub}" or FAIL_CMD == sub:
                sys.exit(1)
            state = load_state()
            cols = state["collections"]
            docs = state["docs"]

            if sub == "list":
                if not cols:
                    print("No collections found.")
                    return
                print(f"Collections ({len(cols)}):")
                for name in sorted(cols):
                    print(f"{name} (qmd://{name}/)")
                return

            if sub == "show":
                if len(argv) < 3 or argv[2] not in cols:
                    print("Collection not found", file=sys.stderr)
                    sys.exit(1)
                info = cols[argv[2]]
                print(f"Collection: {argv[2]}")
                print(f"  Path: {info['path']}")
                print(f"  Pattern: {info['mask']}")
                return

            if sub == "add":
                path = argv[2]
                name = None
                mask = "**/*.md"
                i = 3
                while i < len(argv):
                    if argv[i] == "--name" and i + 1 < len(argv):
                        name = argv[i + 1]
                        i += 2
                    elif argv[i] == "--mask" and i + 1 < len(argv):
                        mask = argv[i + 1]
                        i += 2
                    else:
                        i += 1
                if not name or not Path(path).is_dir():
                    sys.exit(1)
                if name in cols:
                    sys.exit(1)
                for existing in cols.values():
                    if existing["path"] == path and existing["mask"] == mask:
                        sys.exit(1)
                cols[name] = {"path": path, "mask": mask}
                docs[name] = scan_collection(path, mask)
                save_state(state)
                return

            if sub == "remove":
                if len(argv) < 3 or argv[2] not in cols:
                    sys.exit(1)
                del cols[argv[2]]
                docs.pop(argv[2], None)
                save_state(state)
                return

            sys.exit(1)

        if argv[0] == "update":
            if FAIL_CMD == "update":
                sys.exit(1)
            state = load_state()
            for name, info in state["collections"].items():
                state["docs"][name] = scan_collection(info["path"], info["mask"])
            save_state(state)
            return

        if argv[0] == "embed":
            if FAIL_CMD == "embed":
                sys.exit(1)
            return

        if argv[0] == "search":
            if FAIL_CMD == "search":
                sys.exit(1)
            query = argv[1] if len(argv) > 1 else ""
            limit = 10
            if "-n" in argv:
                idx = argv.index("-n")
                if idx + 1 < len(argv):
                    limit = int(argv[idx + 1])
            state = load_state()
            hits = []
            for name in sorted(state["docs"]):
                for rel, body in sorted(state["docs"][name].items()):
                    if query.lower() in body.lower():
                        hits.append(f"qmd://{name}/{rel}")
            if not hits:
                print("No results found.")
                return
            for hit in hits[:limit]:
                print(hit)
            return

        sys.exit(1)


    if __name__ == "__main__":
        main()
    """
)


def _make_qmd_stub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "qmd 2.8.3",
    fail: str = "",
    sleep_s: float = 0.0,
) -> SimpleNamespace:
    bin_dir = tmp_path / "qmd-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "qmd"
    script.write_text(f"#!{sys.executable}\n{_QMD_STUB_SOURCE}", encoding="utf-8")
    script.chmod(0o755)
    state_path = tmp_path / "qmd-state.json"
    log_path = tmp_path / "qmd-calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("QMD_STUB_STATE", str(state_path))
    monkeypatch.setenv("QMD_STUB_LOG", str(log_path))
    monkeypatch.setenv("QMD_STUB_VERSION", version)
    monkeypatch.setenv("QMD_STUB_FAIL", fail)
    monkeypatch.setenv("QMD_STUB_SLEEP", str(sleep_s) if sleep_s else "")

    def calls() -> list[list[str]]:
        if not log_path.exists():
            return []
        return [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def state() -> dict:
        if not state_path.exists():
            return {"collections": {}, "docs": {}}
        return json.loads(state_path.read_text(encoding="utf-8"))

    def search(query: str, n: int = 10) -> str:
        proc = subprocess.run(
            [str(script), "search", query, "-n", str(n)],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    def seed_collection(name: str, path: Path, mask: str = "**/*.md") -> None:
        st = state()
        st["collections"][name] = {"path": str(path), "mask": mask}
        st["docs"][name] = {}
        state_path.write_text(json.dumps(st, sort_keys=True), encoding="utf-8")

    return SimpleNamespace(
        bin_dir=bin_dir,
        script=script,
        state_path=state_path,
        log_path=log_path,
        calls=calls,
        state=state,
        search=search,
        seed_collection=seed_collection,
    )


def _setup_device_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    device_id: str = "dev11111",
    device_name: str = "Mac A",
    enabled: bool = False,
    embeddings: bool = False,
    sources: list[dict] | None = None,
) -> SimpleNamespace:
    home = tmp_path / f"home-{device_id}"
    home.mkdir(parents=True, exist_ok=True)
    cfg_dir = home / ".config" / "mind-meld"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.toml"
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    state_dir = home / ".local" / "share" / "mind-meld"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda _h=home: _h)
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config, "LOCK_PATH", cfg_dir / "mind-meld.lock")
    monkeypatch.setattr(statedb, "DB_DIR", state_dir)
    monkeypatch.setattr(statedb, "db_path", lambda _p=state_dir / "state.db": _p)
    monkeypatch.setattr(statedb, "deleted_copies_dir", lambda _d=state_dir / "deleted": _d)
    monkeypatch.setattr(sidecar, "SIDECAR_DIR", cfg_dir / "sidecar")

    cfg: dict = {
        "device": {"id": device_id, "name": device_name},
        "storage": {"path": str(storage_dir)},
        "crypto": {"argon2_memory_kb": MEMORY_KB},
        "sync": {"max_file_size": 52_428_800},
    }
    if sources is not None:
        cfg["sync"]["sources"] = sources
    if enabled or embeddings:
        cfg["memory_index"] = {"enabled": enabled, "embeddings": embeddings}
    config.save_config(cfg, cfg_path)
    return SimpleNamespace(
        home=home,
        cfg_dir=cfg_dir,
        cfg_path=cfg_path,
        storage_dir=storage_dir,
        state_dir=state_dir,
        cfg=config.load_config(cfg_path),
    )


# ── T1: Config defaults, slugs, desired collections, refresh, command ────────


def test_config_memory_index_defaults_and_lenient_normalization(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[device]\nid = "dev1"\nname = "Mac A"\n[storage]\npath = "~/Sync/mind-meld"\n',
        encoding="utf-8",
    )
    loaded = config.load_config(cfg_path)
    assert loaded["memory_index"] == {"enabled": False, "embeddings": False}
    assert config.memory_index_enabled(loaded) is False
    assert config.memory_index_embeddings(loaded) is False

    # Non-bool values degrade to False rather than raising.
    cfg_path.write_text(
        '[device]\nid = "dev1"\nname = "Mac A"\n[storage]\npath = "~/Sync/mind-meld"\n'
        '[memory_index]\nenabled = "true"\nembeddings = 1\n',
        encoding="utf-8",
    )
    loaded2 = config.load_config(cfg_path)
    assert loaded2["memory_index"] == {"enabled": False, "embeddings": False}

    # Non-dict [memory_index] degrades to False.
    cfg_path.write_text(
        'memory_index = "broken"\n[device]\nid = "dev1"\nname = "Mac A"\n'
        '[storage]\npath = "~/Sync/mind-meld"\n',
        encoding="utf-8",
    )
    loaded3 = config.load_config(cfg_path)
    assert loaded3["memory_index"] == {"enabled": False, "embeddings": False}

    # Raw dict readers never crash.
    assert config.memory_index_enabled(None) is False
    assert config.memory_index_enabled({"memory_index": {"enabled": True}}) is True
    assert config.memory_index_embeddings({"memory_index": {"embeddings": True}}) is True

    # patch_config_on_disk preserves tilde paths and unrelated sections.
    cfg_path.write_text(
        '[device]\nid = "dev1"\nname = "Mac A"\n[storage]\npath = "~/Sync/mind-meld"\n'
        "[memory_index]\nembeddings = true\n",
        encoding="utf-8",
    )
    config.patch_config_on_disk({"memory_index": {"enabled": True}}, cfg_path)
    raw = cfg_path.read_text(encoding="utf-8")
    assert "~/Sync/mind-meld" in raw
    reloaded = config.load_config(cfg_path)
    assert reloaded["memory_index"] == {"enabled": True, "embeddings": True}


def test_slug_sanitization_and_fallbacks() -> None:
    assert memoryindex._slug("\x1b[31mMac Studio (Home)!\x1b[0m", "abcd1234") == "mac-studio-home"
    assert memoryindex._slug("🚀🔥", "dev-99") == "dev-99"
    assert memoryindex._slug("!!!", "???") == "machine"
    long_slug = memoryindex._slug("a" * 100, "fallback")
    assert len(long_slug) == 64
    assert long_slug == "a" * 64


def test_desired_collections_matrix_and_collision_disambiguation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="aaaa1111",
        device_name="Workstation",
        enabled=True,
    )
    claude_dir = tmp_path / "custom-claude"
    (claude_dir / "projects").mkdir(parents=True)
    grok_dir = tmp_path / "custom-grok"
    (grok_dir / "memory-v2").mkdir(parents=True)
    muse_dir = tmp_path / "custom-muse"
    (muse_dir / "memory").mkdir(parents=True)
    codex_dir = tmp_path / "custom-codex"
    (codex_dir / "memories").mkdir(parents=True)
    (codex_dir / "memories_v2").mkdir(parents=True)

    # Register peers: one collides with own name ("Workstation"), one is unique.
    backend = local_storage.LocalBackend(env.storage_dir)
    devices.register_device(backend, "bbbb2222", "Workstation")
    devices.register_device(backend, "cccc3333", "Linux Box")

    copies_root = env.home / ".local" / "share" / "mind-meld" / "codex-memory"
    (copies_root / "bbbb2222").mkdir(parents=True)
    (copies_root / "cccc3333").mkdir(parents=True)
    (copies_root / "dddd4444").mkdir(parents=True)  # not in registry -> fallback to device id

    cfg = {
        **env.cfg,
        "sync": {
            "max_file_size": 52_428_800,
            "sources": [
                {"name": "claude", "path": str(claude_dir), "type": "claude"},
                {"name": "grok", "path": str(grok_dir), "type": "grok"},
                {"name": "muse", "path": str(muse_dir), "type": "muse"},
                {"name": "codex-memory", "path": str(codex_dir), "type": "codex-memory"},
            ],
        },
    }
    cols = {c.name: c for c in memoryindex.desired_collections(cfg)}
    assert set(cols) == {
        "mm-claude",
        "mm-grok",
        "mm-muse",
        "mm-codex-workstation-aaaa1111",
        "mm-codex-workstation-bbbb2222",
        "mm-codex-linux-box",
        "mm-codex-dddd4444",
    }
    assert cols["mm-claude"].mask == "*/memory/**/*.md"
    assert cols["mm-grok"].mask == "**/topics/**/*.md"
    assert cols["mm-muse"].mask == "**/*.md"
    # memories_v2 wins over memories.
    assert cols["mm-codex-workstation-aaaa1111"].path.endswith("memories_v2")
    assert (
        cols["mm-codex-workstation-aaaa1111"].mask
        == "{MEMORY.md,memory_summary.md,rollout_summaries/*.md}"
    )
    assert cols["mm-codex-linux-box"].mask == "**/*.md"

    # Removing memories_v2 falls back to memories.
    (codex_dir / "memories_v2").rmdir()
    cols2 = {c.name: c for c in memoryindex.desired_collections(cfg)}
    assert cols2["mm-codex-workstation-aaaa1111"].path.endswith("memories")

    # Disabled source is omitted.
    cfg_disabled = {**cfg, "sync": {**cfg["sync"], "disabled_sources": ["grok", "muse"]}}
    cols3 = {c.name for c in memoryindex.desired_collections(cfg_disabled)}
    assert "mm-grok" not in cols3
    assert "mm-muse" not in cols3
    assert "mm-claude" in cols3

    # Symlinked copies root yields no peer collections without crashing.
    real_other = tmp_path / "elsewhere"
    real_other.mkdir()
    for sub in list(copies_root.iterdir()):
        sub.rmdir()
    copies_root.rmdir()
    copies_root.symlink_to(real_other)
    cols4 = {c.name for c in memoryindex.desired_collections(cfg)}
    assert "mm-codex-linux-box" not in cols4
    assert "mm-codex-workstation" in cols4


def test_refresh_outcomes_reconcile_embeddings_timeout_and_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _make_qmd_stub(tmp_path, monkeypatch)
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects" / "proj1" / "memory").mkdir(parents=True)
    (claude_dir / "projects" / "proj1" / "memory" / "note.md").write_text(
        "claude-fact-alpha", encoding="utf-8"
    )
    user_dir = tmp_path / "user-notes"
    user_dir.mkdir()
    stub.seed_collection("personal-notes", user_dir)

    # Disabled: skipped-off, no qmd calls, no state file.
    env_off = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="off11111",
        enabled=False,
        sources=[{"name": "claude", "path": str(claude_dir), "type": "claude"}],
    )
    res_off = memoryindex.refresh(env_off.cfg)
    assert res_off.status == "skipped-off"
    assert stub.calls() == []
    assert not memoryindex.state_path().exists()

    # Enabled with embeddings=False: adds mm-claude, preserves personal-notes, no embed.
    env = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="on111111",
        enabled=True,
        embeddings=False,
        sources=[{"name": "claude", "path": str(claude_dir), "type": "claude"}],
    )
    res_ok = memoryindex.refresh(env.cfg)
    assert res_ok.status == "ok"
    assert res_ok.qmd_version == "2.8.3"
    assert res_ok.collections == ("mm-claude",)
    assert "personal-notes" in stub.state()["collections"]
    assert not any(c and c[0] == "embed" for c in stub.calls())
    assert "qmd://mm-claude/proj1/memory/note.md" in stub.search("claude-fact-alpha")
    rec = memoryindex.read_state()
    assert rec["state"] == "recorded"
    assert rec["result"] == "ok"
    assert rec["collections"] == ["mm-claude"]

    # A provider scan failure replaces the last success in the record.
    with monkeypatch.context() as mp:

        def unreadable_source(_cfg):
            raise PermissionError("fixture directory is unreadable")

        mp.setattr(memoryindex, "desired_collections", unreadable_source)
        assert memoryindex.refresh(env.cfg).status == "failed"
        assert memoryindex.read_state()["result"] == "failed"

    # Embeddings=True spawns qmd embed detached.
    popen_calls: list[tuple[list[str], dict]] = []
    real_popen = subprocess.Popen

    def spy_popen(cmd, **kwargs):
        popen_calls.append((list(cmd), kwargs))
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    config.patch_config_on_disk({"memory_index": {"embeddings": True}})
    res_emb = memoryindex.refresh(env.cfg)
    assert res_emb.status == "ok"
    embed_calls = [c for c in popen_calls if c[0][1:] == ["--index", "index", "embed"]]
    assert len(embed_calls) == 1
    assert embed_calls[0][1]["start_new_session"] is True
    assert embed_calls[0][1]["stdin"] is subprocess.DEVNULL
    assert embed_calls[0][1]["stdout"] is subprocess.DEVNULL
    assert embed_calls[0][1]["stderr"] is subprocess.DEVNULL
    monkeypatch.setattr(subprocess, "Popen", real_popen)

    # Single-flight contention: holding flock returns skipped-running without writing.
    sp = memoryindex.state_path()
    fd = os.open(str(sp), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res_busy = memoryindex.refresh(env.cfg)
        assert res_busy.status == "skipped-running"
        off_busy = runner.invoke(cli.app, ["memory-index", "off"])
        assert off_busy.exit_code == 1
        assert "Run: mm memory-index off" in off_busy.output
        assert config.memory_index_enabled(config.load_config(env.cfg_path))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    # Failing qmd update records failed.
    monkeypatch.setenv("QMD_STUB_FAIL", "update")
    res_fail = memoryindex.refresh(env.cfg)
    assert res_fail.status == "failed"
    assert memoryindex.read_state()["result"] == "failed"
    monkeypatch.setenv("QMD_STUB_FAIL", "")

    # Old qmd version records failed.
    monkeypatch.setenv("QMD_STUB_VERSION", "qmd 2.8.2")
    res_old = memoryindex.refresh(env.cfg)
    assert res_old.status == "failed"
    assert memoryindex.read_state()["result"] == "failed"
    monkeypatch.setenv("QMD_STUB_VERSION", "qmd 2.8.3")

    # Timeout records timed-out.
    monkeypatch.setattr(memoryindex, "REFRESH_DEADLINE_S", 0.15)
    monkeypatch.setenv("QMD_STUB_SLEEP", "0.5")
    res_to = memoryindex.refresh(env.cfg)
    assert res_to.status == "timed-out"
    assert memoryindex.read_state()["result"] == "timed-out"
    monkeypatch.setenv("QMD_STUB_SLEEP", "")
    monkeypatch.setattr(memoryindex, "REFRESH_DEADLINE_S", 30.0)

    # Missing qmd records qmd-missing.
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    res_miss = memoryindex.refresh(env.cfg)
    assert res_miss.status == "qmd-missing"
    assert memoryindex.read_state()["result"] == "qmd-missing"


def test_memory_index_command_bare_on_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _make_qmd_stub(tmp_path, monkeypatch)
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects" / "p" / "memory").mkdir(parents=True)
    user_dir = tmp_path / "user-docs"
    user_dir.mkdir()
    stub.seed_collection("keep-me", user_dir)

    env = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="cmd11111",
        enabled=False,
        embeddings=True,
        sources=[{"name": "claude", "path": str(claude_dir), "type": "claude"}],
    )

    # Bare command with missing qmd exits 0 and writes nothing.
    empty_bin = tmp_path / "no-qmd"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    r_bare_miss = runner.invoke(cli.app, ["memory-index"])
    assert r_bare_miss.exit_code == 0, r_bare_miss.output
    assert "enabled:       off" in r_bare_miss.output
    assert "not found" in r_bare_miss.output
    assert not memoryindex.state_path().exists()

    # `on` with missing qmd fails and leaves config untouched.
    r_on_miss = runner.invoke(cli.app, ["memory-index", "on"])
    assert r_on_miss.exit_code == 1
    assert config.memory_index_enabled(config.load_config(env.cfg_path)) is False

    # Restore stub, test old qmd version leaves config untouched.
    monkeypatch.setenv("PATH", f"{stub.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("QMD_STUB_VERSION", "qmd 2.7.0")
    r_on_old = runner.invoke(cli.app, ["memory-index", "on"])
    assert r_on_old.exit_code == 1
    assert config.memory_index_enabled(config.load_config(env.cfg_path)) is False
    monkeypatch.setenv("QMD_STUB_VERSION", "qmd 2.8.3")

    # `on` succeeds, preserves embeddings=True, prints outcome + off fix.
    r_on = runner.invoke(cli.app, ["memory-index", "on"])
    assert r_on.exit_code == 0, r_on.output
    assert "Memory-Index:" in r_on.output
    assert "Run: mm memory-index off" in r_on.output
    reloaded = config.load_config(env.cfg_path)
    assert reloaded["memory_index"] == {"enabled": True, "embeddings": True}
    assert "mm-claude" in stub.state()["collections"]
    assert "keep-me" in stub.state()["collections"]

    # Bare form now shows on, version, mm-claude (not keep-me), and last refresh.
    r_bare = runner.invoke(cli.app, ["memory-index"])
    assert r_bare.exit_code == 0, r_bare.output
    assert "enabled:       on" in r_bare.output
    assert "mm-claude" in r_bare.output
    assert "keep-me" not in r_bare.output
    assert stub.calls()[-2:] == [["--version"], ["collection", "show", "mm-claude"]]

    # `off` disables config, removes only mm- collections, preserves embeddings=True.
    stale_config = config.load_config(env.cfg_path)
    r_off = runner.invoke(cli.app, ["memory-index", "off"])
    assert r_off.exit_code == 0, r_off.output
    reloaded2 = config.load_config(env.cfg_path)
    assert reloaded2["memory_index"] == {"enabled": False, "embeddings": True}
    assert "mm-claude" not in stub.state()["collections"]
    assert "keep-me" in stub.state()["collections"]
    assert memoryindex.refresh(stale_config).status == "skipped-off"
    assert "mm-claude" not in stub.state()["collections"]

    # Already-off no-op.
    r_off2 = runner.invoke(cli.app, ["memory-index", "off"])
    assert r_off2.exit_code == 0, r_off2.output
    assert "already off" in r_off2.output

    # Invalid action exits with the documented usage error.
    r_bad = runner.invoke(cli.app, ["memory-index", "bogus"])
    assert r_bad.exit_code == 2


# ── T2: Attended push/pull tails, two-machine Codex search, hooks ────────────


def _init_two_machine_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    storage_dir = tmp_path / "fleet-storage"
    backend = local_storage.LocalBackend(storage_dir)
    boot = crypto.bootstrap_crypto_init(backend, PASSPHRASE, MEMORY_KB)
    crypto.set_crypto_session(boot.root_salt, MEMORY_KB)

    devs: dict[str, SimpleNamespace] = {}
    for did, dname in (("aaaaaaaa", "Mac A"), ("bbbbbbbb", "Linux B")):
        devices.register_device(backend, did, dname)
        devices.update_last_seen(backend, did)
        dev_root = tmp_path / f"dev-{did}"
        home = dev_root / "home"
        codex_dir = home / ".codex"
        codex_dir.mkdir(parents=True)
        cfg_dir = home / ".config" / "mind-meld"
        cfg_dir.mkdir(parents=True)
        cfg_path = cfg_dir / "config.toml"
        state_dir = home / ".local" / "share" / "mind-meld"
        state_dir.mkdir(parents=True)
        copies_dir = state_dir / "codex-memory"
        cfg = {
            "device": {"id": did, "name": dname},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "codex-memory", "path": str(codex_dir), "type": "codex-memory"}
                ],
            },
            "crypto": {"argon2_memory_kb": MEMORY_KB},
            "upgrade": {"auto_check": False, "auto_install": False},
            "memory_index": {"enabled": True, "embeddings": False},
        }
        config.save_config(cfg, cfg_path)
        devs[did] = SimpleNamespace(
            id=did,
            name=dname,
            home=home,
            codex=codex_dir,
            cfg_dir=cfg_dir,
            cfg_path=cfg_path,
            state_dir=state_dir,
            copies_dir=copies_dir,
            lock_path=cfg_dir / "mind-meld.lock",
        )

    def activate(did: str) -> SimpleNamespace:
        dev = devs[did]
        monkeypatch.setenv("HOME", str(dev.home))
        monkeypatch.setattr(Path, "home", lambda _h=dev.home: _h)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        monkeypatch.setattr(config, "CONFIG_DIR", dev.cfg_dir)
        monkeypatch.setattr(config, "CONFIG_PATH", dev.cfg_path)
        monkeypatch.setattr(config, "LOCK_PATH", dev.lock_path)
        monkeypatch.setattr(statedb, "DB_DIR", dev.state_dir)
        monkeypatch.setattr(statedb, "db_path", lambda _p=dev.state_dir / "state.db": _p)
        monkeypatch.setattr(statedb, "deleted_copies_dir", lambda _d=dev.state_dir / "deleted": _d)
        monkeypatch.setattr(sidecar, "SIDECAR_DIR", dev.cfg_dir / "sidecar")
        return dev

    return SimpleNamespace(storage_dir=storage_dir, backend=backend, devs=devs, activate=activate)


def test_two_machine_attended_push_pull_refreshes_qmd_after_add_edit_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = _init_two_machine_env(tmp_path, monkeypatch)
    stub_a = _make_qmd_stub(tmp_path / "qmd-a", monkeypatch)
    stub_b = _make_qmd_stub(tmp_path / "qmd-b", monkeypatch)

    # Machine A writes allowed Codex memory files plus excluded files.
    dev_a = fleet.activate("aaaaaaaa")
    monkeypatch.setenv("PATH", f"{stub_a.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("QMD_STUB_STATE", str(stub_a.state_path))
    monkeypatch.setenv("QMD_STUB_LOG", str(stub_a.log_path))
    mem_a = dev_a.codex / "memories_v2"
    (mem_a / "rollout_summaries").mkdir(parents=True)
    (mem_a / "MEMORY.md").write_text("alpha-memory-token-159a\n", encoding="utf-8")
    (mem_a / "rollout_summaries" / "r1.md").write_text("rollout-token-159a\n", encoding="utf-8")
    (mem_a / "raw_memories.md").write_text("raw-secret-must-not-index\n", encoding="utf-8")
    (mem_a / "extensions").mkdir()
    (mem_a / "extensions" / "ext.md").write_text("ext-must-not-index\n", encoding="utf-8")

    r_push = runner.invoke(cli.app, ["push"])
    assert r_push.exit_code == 0, r_push.output
    assert "qmd://mm-codex-mac-a/MEMORY.md" in stub_a.search("alpha-memory-token-159a")
    assert "qmd://mm-codex-mac-a/rollout_summaries/r1.md" in stub_a.search("rollout-token-159a")
    assert "No results found." in stub_a.search("raw-secret-must-not-index")
    assert "No results found." in stub_a.search("ext-must-not-index")

    # Machine B pulls and searches for A's memory.
    dev_b = fleet.activate("bbbbbbbb")
    monkeypatch.setenv("PATH", f"{stub_b.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("QMD_STUB_STATE", str(stub_b.state_path))
    monkeypatch.setenv("QMD_STUB_LOG", str(stub_b.log_path))

    r_pull = runner.invoke(cli.app, ["pull"])
    assert r_pull.exit_code == 0, r_pull.output
    assert "qmd://mm-codex-mac-a/MEMORY.md" in stub_b.search("alpha-memory-token-159a")

    # A change in the same file must replace the indexed text on B.
    fleet.activate("aaaaaaaa")
    monkeypatch.setenv("PATH", f"{stub_a.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("QMD_STUB_STATE", str(stub_a.state_path))
    monkeypatch.setenv("QMD_STUB_LOG", str(stub_a.log_path))
    (mem_a / "MEMORY.md").write_text("updated-memory-token-159a\n", encoding="utf-8")
    updated_push = runner.invoke(cli.app, ["push"])
    assert updated_push.exit_code == 0, updated_push.output

    fleet.activate("bbbbbbbb")
    monkeypatch.setenv("PATH", f"{stub_b.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("QMD_STUB_STATE", str(stub_b.state_path))
    monkeypatch.setenv("QMD_STUB_LOG", str(stub_b.log_path))
    updated_pull = runner.invoke(cli.app, ["pull"])
    assert updated_pull.exit_code == 0, updated_pull.output
    assert "qmd://mm-codex-mac-a/MEMORY.md" in stub_b.search("updated-memory-token-159a")
    assert "No results found." in stub_b.search("alpha-memory-token-159a")

    # Machine A deletes MEMORY.md and pushes; Machine B pulls; search reflects deletion.
    dev_a = fleet.activate("aaaaaaaa")
    monkeypatch.setenv("PATH", f"{stub_a.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("QMD_STUB_STATE", str(stub_a.state_path))
    monkeypatch.setenv("QMD_STUB_LOG", str(stub_a.log_path))
    (mem_a / "MEMORY.md").unlink()
    r_push2 = runner.invoke(cli.app, ["push"])
    assert r_push2.exit_code == 0, r_push2.output

    dev_b = fleet.activate("bbbbbbbb")
    monkeypatch.setenv("PATH", f"{stub_b.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("QMD_STUB_STATE", str(stub_b.state_path))
    monkeypatch.setenv("QMD_STUB_LOG", str(stub_b.log_path))
    r_pull2 = runner.invoke(cli.app, ["pull"])
    assert r_pull2.exit_code == 0, r_pull2.output
    assert "No results found." in stub_b.search("updated-memory-token-159a")

    # Removing B's peer copies folder and refreshing removes A's peer collection.
    peer_dir = dev_b.copies_dir / "aaaaaaaa"
    for child in list(peer_dir.rglob("*")):
        if child.is_file():
            child.unlink()
    for child in sorted(peer_dir.rglob("*"), reverse=True):
        if child.is_dir():
            child.rmdir()
    peer_dir.rmdir()
    res = memoryindex.refresh(config.load_config(dev_b.cfg_path))
    assert res.status == "ok"
    assert "mm-codex-mac-a" not in stub_b.state()["collections"]


def test_attended_lock_order_dry_run_failure_and_missing_qmd_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = _init_two_machine_env(tmp_path, monkeypatch)
    dev_a = fleet.activate("aaaaaaaa")
    (dev_a.codex / "memories_v2").mkdir(parents=True)
    (dev_a.codex / "memories_v2" / "MEMORY.md").write_text("hi\n", encoding="utf-8")

    # Lock order on push and pull: release precedes refresh.
    events: list[str] = []
    real_release = core._release_sync_lock

    def spy_release():
        events.append("release")
        return real_release()

    def spy_refresh(cfg):
        events.append("refresh")
        return memoryindex.RefreshResult(status="ok")

    with monkeypatch.context() as mp:
        mp.setattr(core, "_release_sync_lock", spy_release)
        mp.setattr(memoryindex, "refresh", spy_refresh)

        r_push = runner.invoke(cli.app, ["push"])
        assert r_push.exit_code == 0, r_push.output
        assert events == ["release", "refresh"]

        events.clear()
        r_push_dry = runner.invoke(cli.app, ["push", "--dry-run"])
        assert r_push_dry.exit_code == 0, r_push_dry.output
        assert events == ["release"]

        events.clear()
        r_pull = runner.invoke(cli.app, ["pull"])
        assert r_pull.exit_code == 0, r_pull.output
        assert events == ["release", "refresh"]

        events.clear()
        r_pull_dry = runner.invoke(cli.app, ["pull", "--dry-run"])
        assert r_pull_dry.exit_code == 0, r_pull_dry.output
        assert events == ["release"]

        events.clear()
        mp.setattr(pullflow, "_pull_core", lambda *a, **kw: pullflow.PullResult(total_failed=1))
        r_pull_failed = runner.invoke(cli.app, ["pull"])
        assert r_pull_failed.exit_code == 1, r_pull_failed.output
        assert events == ["release"]

    # Missing qmd never changes push/pull exit code or adds output lines.
    fleet2 = _init_two_machine_env(tmp_path / "f2", monkeypatch)
    dev_a2 = fleet2.activate("aaaaaaaa")
    empty_bin = tmp_path / "no-qmd-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    r_push_miss = runner.invoke(cli.app, ["push"])
    assert r_push_miss.exit_code == 0, r_push_miss.output
    assert "qmd" not in r_push_miss.output.lower()
    assert memoryindex.read_state()["result"] == "qmd-missing"

    # Snapshot-refused push does not refresh.
    refresh_called = False

    def fail_refresh(_cfg):
        nonlocal refresh_called
        refresh_called = True
        return memoryindex.RefreshResult(status="ok")

    monkeypatch.setattr(memoryindex, "refresh", fail_refresh)
    monkeypatch.setattr(
        cli,
        "_push_core",
        lambda *a, **kw: (_ for _ in ()).throw(cli.SnapshotError("refused for test")),
    )
    r_refused = runner.invoke(cli.app, ["push"])
    assert r_refused.exit_code == 1
    assert refresh_called is False
    assert dev_a2.cfg_path.exists()


def test_hooks_spawn_detached_after_lock_release_and_child_records_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = _init_two_machine_env(tmp_path, monkeypatch)
    dev_a = fleet.activate("aaaaaaaa")
    (dev_a.codex / "memories_v2").mkdir(parents=True)
    (dev_a.codex / "memories_v2" / "MEMORY.md").write_text("hook-mem\n", encoding="utf-8")

    events: list[str] = []
    real_release = cli.release_lock

    def spy_release():
        events.append("release")
        return real_release()

    def spy_spawn(cfg):
        events.append("spawn")
        return True

    with monkeypatch.context() as mp:
        mp.setattr(cli, "release_lock", spy_release)
        mp.setattr(memoryindex, "start_detached_refresh", spy_spawn)

        r_apush = runner.invoke(cli.app, ["autopush"])
        assert r_apush.exit_code == 0, r_apush.output
        assert r_apush.output == ""
        assert events == ["release", "spawn"]

        events.clear()
        r_apull = runner.invoke(cli.app, ["autopull"])
        assert r_apull.exit_code == 0, r_apull.output
        assert r_apull.output == ""
        assert events == ["release", "spawn"]

        events.clear()
        mp.setattr(pullflow, "_pull_core", lambda *a, **kw: pullflow.PullResult(total_failed=1))
        r_apull_failed = runner.invoke(cli.app, ["autopull"])
        assert "1 file(s) failed to apply" in r_apull_failed.output
        assert events == ["release"]

    # Unit-test start_detached_refresh Popen arguments.
    popen_args: list[tuple[list[str], dict]] = []

    class _FakeProc:
        def wait(self, *a, **kw):
            raise AssertionError("start_detached_refresh must never call wait")

        def communicate(self, *a, **kw):
            raise AssertionError("start_detached_refresh must never call communicate")

    with monkeypatch.context() as mp:
        mp.setattr(
            subprocess,
            "Popen",
            lambda cmd, **kw: (popen_args.append((list(cmd), kw)), _FakeProc())[1],
        )
        assert memoryindex.start_detached_refresh(config.load_config(dev_a.cfg_path)) is True
    assert popen_args[0][0] == [sys.executable, "-P", "-m", "mind_meld.memoryindex"]
    assert popen_args[0][1]["cwd"] == "/"
    assert popen_args[0][1]["start_new_session"] is True
    assert popen_args[0][1]["stdin"] is subprocess.DEVNULL
    assert popen_args[0][1]["stdout"] is subprocess.DEVNULL
    assert popen_args[0][1]["stderr"] is subprocess.DEVNULL

    # Real detached child subprocess under temporary HOME with stub on PATH.
    stub = _make_qmd_stub(tmp_path / "child-qmd", monkeypatch)
    child_home = tmp_path / "child-home"
    (child_home / ".config" / "mind-meld").mkdir(parents=True)
    (child_home / ".claude" / "projects" / "p" / "memory").mkdir(parents=True)
    (child_home / ".claude" / "projects" / "p" / "memory" / "m.md").write_text(
        "child-token", encoding="utf-8"
    )
    child_storage = tmp_path / "child-storage"
    child_storage.mkdir()
    child_cfg = {
        "device": {"id": "ch111111", "name": "Child Box"},
        "storage": {"path": str(child_storage)},
        "sync": {
            "max_file_size": 52_428_800,
            "sources": [{"name": "claude", "path": str(child_home / ".claude"), "type": "claude"}],
        },
        "crypto": {"argon2_memory_kb": MEMORY_KB},
        "memory_index": {"enabled": True, "embeddings": False},
    }
    config.save_config(child_cfg, child_home / ".config" / "mind-meld" / "config.toml")
    child_env = {
        **os.environ,
        "HOME": str(child_home),
        "PATH": f"{stub.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "QMD_STUB_STATE": str(stub.state_path),
        "QMD_STUB_LOG": str(stub.log_path),
        "QMD_STUB_VERSION": "qmd 2.8.3",
        "QMD_STUB_FAIL": "",
        "QMD_STUB_SLEEP": "",
    }
    child_env.pop("PYTEST_CURRENT_TEST", None)
    proc = subprocess.run(
        [sys.executable, "-m", "mind_meld.memoryindex"],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    rec_path = child_home / ".config" / "mind-meld" / "memory-index.json"
    assert rec_path.exists()
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    assert rec["result"] == "ok"
    assert rec["collections"] == ["mm-claude"]


# ── T3: mm status and mm diag ────────────────────────────────────────────────


def test_status_memory_index_reporting_and_no_qmd_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _make_qmd_stub(tmp_path, monkeypatch)
    env = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="st111111",
        device_name="Mac Status",
        enabled=True,
        sources=[],
    )
    backend = local_storage.LocalBackend(env.storage_dir)
    boot = crypto.bootstrap_crypto_init(backend, PASSPHRASE, MEMORY_KB)
    crypto.set_crypto_session(boot.root_salt, MEMORY_KB)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    devices.register_device(backend, "st111111", "Mac Status")

    # Missing state file -> one status line with fix, zero qmd calls.
    r_miss = runner.invoke(cli.app, ["status"])
    assert r_miss.exit_code == 0, r_miss.output
    mi_lines = [ln for ln in r_miss.output.splitlines() if "Memory index:" in ln]
    assert len(mi_lines) == 1
    assert "Run: mm memory-index on" in mi_lines[0]
    assert stub.calls() == []

    # Recorded failure states each print one line with fix, zero qmd calls.
    for result_val, expected_fix in (
        ("qmd-missing", "install qmd or run mm memory-index off"),
        ("failed", "mm memory-index on"),
        ("timed-out", "mm memory-index on"),
    ):
        memoryindex.state_path().write_text(
            json.dumps(
                {
                    "version": 1,
                    "result": result_val,
                    "at": "2026-11-21T12:00:00+00:00",
                    "qmd_version": None,
                    "collections": [],
                }
            ),
            encoding="utf-8",
        )
        r = runner.invoke(cli.app, ["status"])
        assert r.exit_code == 0, r.output
        mi_lines = [ln for ln in r.output.splitlines() if "Memory index:" in ln]
        assert len(mi_lines) == 1
        assert f"Run: {expected_fix}" in mi_lines[0]
        assert stub.calls() == []

    # Malformed state prints one line with fix.
    memoryindex.state_path().write_text("{not-json", encoding="utf-8")
    r_mal = runner.invoke(cli.app, ["status"])
    assert r_mal.exit_code == 0, r_mal.output
    assert len([ln for ln in r_mal.output.splitlines() if "Memory index:" in ln]) == 1

    # Contended lock prints nothing.
    memoryindex.state_path().write_text(
        json.dumps(
            {
                "version": 1,
                "result": "failed",
                "at": "2026-11-21T12:00:00+00:00",
                "qmd_version": "2.8.3",
                "collections": [],
            }
        ),
        encoding="utf-8",
    )
    fd = os.open(str(memoryindex.state_path()), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r_lock = runner.invoke(cli.app, ["status"])
        assert r_lock.exit_code == 0, r_lock.output
        assert "Memory index:" not in r_lock.output
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    # Recorded ok prints nothing.
    memoryindex.state_path().write_text(
        json.dumps(
            {
                "version": 1,
                "result": "ok",
                "at": "2026-11-21T12:00:00+00:00",
                "qmd_version": "2.8.3",
                "collections": ["mm-claude"],
            }
        ),
        encoding="utf-8",
    )
    r_ok = runner.invoke(cli.app, ["status"])
    assert r_ok.exit_code == 0, r_ok.output
    assert "Memory index:" not in r_ok.output
    assert stub.calls() == []


def test_diag_memory_index_json_off_on_and_sanitization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _make_qmd_stub(tmp_path, monkeypatch)

    # Off: no qmd subprocess, memory_index shows disabled.
    env_off = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="dg000001",
        enabled=False,
        sources=[],
    )
    r_off = runner.invoke(cli.app, ["diag", "--json"])
    assert r_off.exit_code == 0, r_off.output
    data_off = json.loads(r_off.output)
    assert data_off["memory_index"]["enabled"] is False
    assert data_off["memory_index"]["qmd_version"] is None
    assert data_off["memory_index"]["collections"] == []
    assert stub.calls() == []
    assert env_off.cfg_path.exists()

    # Enabled: probes qmd for version and mm- collections.
    claude_dir = tmp_path / "diag-claude"
    (claude_dir / "projects").mkdir(parents=True)
    env_on = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="dg000002",
        enabled=True,
        sources=[{"name": "claude", "path": str(claude_dir), "type": "claude"}],
    )
    assert memoryindex.refresh(env_on.cfg).status == "ok"
    r_on = runner.invoke(cli.app, ["diag", "--json"])
    assert r_on.exit_code == 0, r_on.output
    data_on = json.loads(r_on.output)
    mi = data_on["memory_index"]
    assert mi["enabled"] is True
    assert mi["qmd_version"] == "2.8.3"
    assert mi["collections"] == ["mm-claude"]
    assert mi["last_refresh"]["state"] == "recorded"
    assert mi["last_refresh"]["result"] == "ok"

    # Plain-text diag sanitizes hostile collection names.
    hostile = "mm-\x1b[31mevil\x1b[0m[bold]name"
    monkeypatch.setattr(
        memoryindex,
        "diag_state",
        lambda _cfg: {
            "enabled": True,
            "embeddings": False,
            "qmd_version": "2.8.3",
            "collections": [hostile],
            "last_refresh": {
                "state": "recorded",
                "result": "ok",
                "at": "2026-11-21T12:00:00+00:00",
            },
        },
    )
    r_txt = runner.invoke(cli.app, ["diag"])
    assert r_txt.exit_code == 0, r_txt.output
    assert "Memory index" in r_txt.output
    assert "\x1b" not in r_txt.output
    assert "mm-evil[bold]name" in r_txt.output
    assert statusflow._print_memory_index_diag is not None


# ── T4: MCP commands live only in docs, never under src/ ─────────────────────


def test_mcp_add_commands_documented_only_never_in_src() -> None:
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    for cmd in ("claude mcp add qmd -- qmd mcp", "codex mcp add qmd -- qmd mcp"):
        assert cmd in readme
        for py in (root / "src").rglob("*.py"):
            assert cmd not in py.read_text(encoding="utf-8"), f"{py} must not contain {cmd!r}"
