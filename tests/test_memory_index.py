"""Tests for Track 159A: local qmd memory index, sync tails, status, and diag."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import textwrap
import threading
from contextlib import nullcontext
from datetime import datetime, timedelta
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
    lockedjson,
    memoryindex,
    meshflow,
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


    def log_call(selected_index, raw_argv, cmd_argv):
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "index": selected_index,
            "argv": raw_argv,
            "cmd": cmd_argv,
            "cwd": os.getcwd(),
        }
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\\n")


    def load_state():
        if not STATE_PATH.exists():
            return {"indexes": {}}
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if "indexes" not in raw:
            return {"indexes": {"mind-meld": raw}}
        return raw


    def index_bucket(state, index_name):
        return state["indexes"].setdefault(index_name, {"collections": {}, "docs": {}})


    def save_state(state):
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


    def find_project_index():
        cur = Path.cwd().resolve()
        for parent in (cur, *cur.parents):
            candidate = parent / ".qmd" / "index.yml"
            if candidate.is_file():
                return f"project:{candidate}"
        return "index"


    def should_fail(selected_index, *keys):
        if not FAIL_CMD:
            return False
        rules = [r.strip() for r in FAIL_CMD.split(",") if r.strip()]
        for key in keys:
            if key in rules or f"{selected_index}:{key}" in rules:
                return True
        return False


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
        raw_argv = list(sys.argv[1:])
        argv = list(raw_argv)
        selected_index = None
        if len(argv) >= 2 and argv[0] == "--index":
            selected_index = argv[1]
            argv = argv[2:]
        if not argv:
            log_call(selected_index, raw_argv, argv)
            sys.exit(1)
        if argv[0] != "--version" and selected_index is None:
            selected_index = find_project_index()
        log_call(selected_index, raw_argv, argv)
        if SLEEP_S > 0:
            time.sleep(SLEEP_S)

        if argv[0] == "--version":
            if should_fail(selected_index, "version"):
                sys.exit(1)
            print(VERSION)
            return

        if argv[0] == "collection":
            if len(argv) < 2:
                sys.exit(1)
            sub = argv[1]
            target_name = argv[2] if len(argv) >= 3 else ""
            fail_keys = [f"collection {sub}", sub]
            if target_name:
                fail_keys.append(f"collection {sub}:{target_name}")
            if should_fail(selected_index, *fail_keys):
                sys.exit(1)
            state = load_state()
            bucket = index_bucket(state, selected_index)
            cols = bucket["collections"]
            docs = bucket["docs"]

            if sub == "list":
                if "QMD_STUB_LIST_STDOUT" in os.environ:
                    sys.stdout.write(os.environ["QMD_STUB_LIST_STDOUT"])
                    return
                if not cols:
                    print("No collections found.")
                    return
                print(f"Collections ({len(cols)}):")
                for name in sorted(cols):
                    print(f"  {name}  (qmd://{name}/)")
                return

            if sub == "show":
                if "QMD_STUB_SHOW_STDOUT" in os.environ:
                    sys.stdout.write(os.environ["QMD_STUB_SHOW_STDOUT"])
                    return
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
            if should_fail(selected_index, "update"):
                sys.exit(1)
            state = load_state()
            bucket = index_bucket(state, selected_index)
            for name, info in bucket["collections"].items():
                bucket["docs"][name] = scan_collection(info["path"], info["mask"])
            save_state(state)
            return

        if argv[0] == "embed":
            if should_fail(selected_index, "embed"):
                sys.exit(1)
            return

        if argv[0] == "search":
            if should_fail(selected_index, "search"):
                sys.exit(1)
            query = argv[1] if len(argv) > 1 else ""
            limit = 10
            if "-n" in argv:
                idx = argv.index("-n")
                if idx + 1 < len(argv):
                    limit = int(argv[idx + 1])
            state = load_state()
            bucket = index_bucket(state, selected_index)
            hits = []
            for name in sorted(bucket["docs"]):
                for rel, body in sorted(bucket["docs"][name].items()):
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

    def indexed_calls() -> list[dict]:
        if not log_path.exists():
            return []
        return [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def calls() -> list[list[str]]:
        return [entry["cmd"] for entry in indexed_calls()]

    def _load_full_state() -> dict:
        if not state_path.exists():
            return {"indexes": {}}
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if "indexes" not in raw:
            return {"indexes": {"mind-meld": raw}}
        return raw

    def state(index: str = "mind-meld") -> dict:
        full = _load_full_state()
        return full["indexes"].get(index, {"collections": {}, "docs": {}})

    def search(
        query: str,
        n: int = 10,
        *,
        index: str | None = "mind-meld",
        cwd: Path | None = None,
    ) -> str:
        cmd = [str(script)]
        if index is not None:
            cmd.extend(["--index", index])
        cmd.extend(["search", query, "-n", str(n)])
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    def seed_collection(
        name: str,
        path: Path,
        mask: str = "**/*.md",
        *,
        index: str = "mind-meld",
        docs: dict[str, str] | None = None,
    ) -> None:
        full = _load_full_state()
        bucket = full["indexes"].setdefault(index, {"collections": {}, "docs": {}})
        bucket["collections"][name] = {"path": str(path), "mask": mask}
        bucket["docs"][name] = dict(docs) if docs is not None else {}
        state_path.write_text(json.dumps(full, sort_keys=True), encoding="utf-8")

    return SimpleNamespace(
        bin_dir=bin_dir,
        script=script,
        state_path=state_path,
        log_path=log_path,
        calls=calls,
        indexed_calls=indexed_calls,
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
    # Value: protects=peer collections stay within the real copies tree;
    # fails_when=qmd receives a collection for a linked copies root;
    # why_new=an empty symlink target could never produce the unwanted collection;
    # seam=none; no_claim=the fixture checks collection selection before qmd runs.
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

    codex_source = config.get_default_source("codex")
    assert codex_source is not None
    codex_source["path"] = str(codex_dir)
    cfg = {
        **env.cfg,
        "sync": {
            "max_file_size": 52_428_800,
            "sources": [
                {"name": "claude", "path": str(claude_dir), "type": "claude"},
                {"name": "grok", "path": str(grok_dir), "type": "grok"},
                {"name": "muse", "path": str(muse_dir), "type": "muse"},
                codex_source,
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
    (real_other / "cccc3333").mkdir(parents=True)
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
    # Value: protects=refresh reconciles mismatches and records bounded local outcomes;
    # fails_when=it reuses stale collections, updates after reconciliation fails,
    # resets the deadline per call, or persists fields outside the record contract;
    # why_new=existing cases use matching collections, then fail one command;
    # seam=none; no_claim=qmd responses and cumulative elapsed time use fixtures.
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
    stored = json.loads(memoryindex.state_path().read_text(encoding="utf-8"))
    assert stored == {
        "version": 1,
        "index": "mind-meld",
        "result": "ok",
        "at": res_ok.at,
        "qmd_version": "2.8.3",
        "collections": ["mm-claude"],
    }
    assert datetime.fromisoformat(stored["at"]).utcoffset() == timedelta(0)

    projects = claude_dir / "projects"
    claude_mask = "*/memory/**/*.md"
    old_projects = tmp_path / "old-projects"
    old_projects.mkdir()
    for wrong_path, wrong_mask in ((old_projects, claude_mask), (projects, "**/*.md")):
        stub.seed_collection("mm-claude", wrong_path, wrong_mask)
        assert memoryindex.refresh(env.cfg).status == "ok"
        assert stub.state()["collections"]["mm-claude"] == {
            "path": str(projects.resolve()),
            "mask": claude_mask,
        }
        assert "qmd://mm-claude/proj1/memory/note.md" in stub.search("claude-fact-alpha")
        assert "personal-notes" in stub.state()["collections"]

    for subcommand in ("list", "show", "remove", "add"):
        stub.seed_collection("mm-claude", old_projects, claude_mask)
        before = len(stub.calls())
        with monkeypatch.context() as mp:
            mp.setenv("QMD_STUB_FAIL", f"collection {subcommand}")
            assert memoryindex.refresh(env.cfg).status == "failed"
        calls = stub.calls()[before:]
        assert calls[-1][:2] == ["collection", subcommand]
        assert ["update"] not in calls
        assert memoryindex.read_state()["result"] == "failed"

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
    monkeypatch.setenv("INDEX_PATH", str(tmp_path / "separate.sqlite"))
    res_emb = memoryindex.refresh(env.cfg)
    assert res_emb.status == "ok"
    embed_calls = [c for c in popen_calls if c[0][1:] == ["--index", "mind-meld", "embed"]]
    assert len(embed_calls) == 1
    assert embed_calls[0][1]["start_new_session"] is True
    assert embed_calls[0][1]["stdin"] is subprocess.DEVNULL
    assert embed_calls[0][1]["stdout"] is subprocess.DEVNULL
    assert embed_calls[0][1]["stderr"] is subprocess.DEVNULL
    assert all("INDEX_PATH" not in kw["env"] for cmd, kw in popen_calls if "show" not in cmd)
    monkeypatch.setattr(subprocess, "Popen", real_popen)

    # Single-flight contention: holding flock returns skipped-running without writing.
    sp = memoryindex.state_path()
    fd = os.open(str(sp), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res_busy = memoryindex.refresh(env.cfg)
        assert res_busy.status == "skipped-running"
        with monkeypatch.context() as mp:
            mp.setattr(memoryindex, "REFRESH_DEADLINE_S", 0.05)
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

    # Each qmd call needs 20 seconds, so only the first fits in one 30-second budget.
    clock = SimpleNamespace(now=100.0)
    timeouts: list[float] = []
    stopped_groups: list[object] = []
    process_group = object()

    def fixture_qmd(argv, **_kwargs):
        def communicate(*, timeout):
            timeouts.append(timeout)
            if timeout < 20.0:
                raise subprocess.TimeoutExpired(argv, timeout)
            clock.now += 20.0
            output = "qmd 2.8.3" if argv[-1] == "--version" else "No collections found."
            return output, None

        return nullcontext(
            SimpleNamespace(pid=process_group, communicate=communicate, returncode=0)
        )

    def stop_process_group(group, signum):
        assert group is process_group
        assert signum == signal.SIGKILL
        assert memoryindex.read_state()["state"] == "lock_failed"
        stopped_groups.append(group)

    with monkeypatch.context() as mp:
        mp.setattr(memoryindex, "time", SimpleNamespace(monotonic=lambda: clock.now))
        mp.setattr(subprocess, "Popen", fixture_qmd)
        mp.setattr(os, "killpg", stop_process_group)
        assert memoryindex.refresh(env.cfg).status == "timed-out"
    assert timeouts == [30.0, 10.0]
    assert stopped_groups == [process_group]
    assert memoryindex.read_state()["result"] == "timed-out"

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
    # Value: protects=memory-index toggles persist when qmd fails and can retry;
    # fails_when=on rolls back after update failure or off fails to disable and clean up;
    # why_new=the earlier cases only fail on preflight and use working qmd for removal;
    # seam=none; no_claim=qmd command failures and collection storage use a fixture.
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

    # `on` with missing qmd fails with install_hint() and leaves index data untouched.
    config_before = env.cfg_path.read_bytes()
    collections_before = stub.state_path.read_bytes()
    r_on_miss = runner.invoke(cli.app, ["memory-index", "on"])
    assert r_on_miss.exit_code == 1
    assert r_on_miss.stdout == ""
    assert (
        r_on_miss.stderr.splitlines()[0] == "mm: error: could not verify qmd 2.8.3+ (qmd-missing)."
    )
    assert r_on_miss.stderr.splitlines()[-1] == f"Run: {memoryindex.install_hint()}"
    assert config.memory_index_enabled(config.load_config(env.cfg_path)) is False
    assert env.cfg_path.read_bytes() == config_before
    assert stub.state_path.read_bytes() == collections_before
    assert not memoryindex.state_path().exists()

    # Restore stub, test old qmd version fails with install_hint() and leaves config untouched.
    monkeypatch.setenv("PATH", f"{stub.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("QMD_STUB_VERSION", "qmd 2.7.0")
    r_on_old = runner.invoke(cli.app, ["memory-index", "on"])
    assert r_on_old.exit_code == 1
    assert r_on_old.stdout == ""
    assert (
        r_on_old.stderr.splitlines()[0] == "mm: error: could not verify qmd 2.8.3+ (found 2.7.0)."
    )
    assert r_on_old.stderr.splitlines()[-1] == f"Run: {memoryindex.install_hint()}"
    assert config.memory_index_enabled(config.load_config(env.cfg_path)) is False
    monkeypatch.setenv("QMD_STUB_VERSION", "qmd 2.8.3")

    # On persists the owner's choice even when an initial refresh fails.
    with monkeypatch.context() as mp:
        mp.setenv("QMD_STUB_FAIL", "update")
        r_on_fail = runner.invoke(cli.app, ["memory-index", "on"])
    assert r_on_fail.exit_code == 1, r_on_fail.output
    assert "Run: mm memory-index on" in r_on_fail.stderr
    assert config.load_config(env.cfg_path)["memory_index"] == {"enabled": True, "embeddings": True}
    assert memoryindex.read_state()["result"] == "failed"

    # `on` succeeds, preserves embeddings=True, prints outcome with setup link + off fix.
    r_on = runner.invoke(cli.app, ["memory-index", "on"])
    assert r_on.exit_code == 0, r_on.output
    assert "Memory-Index:" in r_on.output
    assert memoryindex.AGENT_SETUP_URL in r_on.output
    assert "Run: mm memory-index off" in r_on.output
    reloaded = config.load_config(env.cfg_path)
    assert reloaded["memory_index"] == {"enabled": True, "embeddings": True}
    assert "mm-claude" in stub.state()["collections"]
    assert "keep-me" in stub.state()["collections"]

    # Bare form now shows on, version, mm-claude (not keep-me), and last refresh.
    observed_files = (env.cfg_path, memoryindex.state_path(), stub.state_path)
    before_bare = [path.read_bytes() for path in observed_files]
    bare_calls: list[list[str]] = []
    real_popen = subprocess.Popen

    def observe_bare_call(argv, **kwargs):
        bare_calls.append(list(argv))
        return real_popen(argv, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(subprocess, "Popen", observe_bare_call)
        r_bare = runner.invoke(cli.app, ["memory-index"])
    assert r_bare.exit_code == 0, r_bare.output
    assert "enabled:       on" in r_bare.output
    assert "mm-claude" in r_bare.output
    assert "keep-me" not in r_bare.output
    assert bare_calls == [
        [str(stub.script), "--version"],
        [str(stub.script), "--index", "mind-meld", "collection", "show", "mm-claude"],
    ]
    assert [path.read_bytes() for path in observed_files] == before_bare

    # Off succeeds without qmd and explains why it could not remove the collection.
    off_with_embeddings = {"enabled": False, "embeddings": True}
    with monkeypatch.context() as mp:
        mp.setenv("PATH", str(empty_bin))
        r_off_missing = runner.invoke(cli.app, ["memory-index", "off"])
    assert r_off_missing.exit_code == 0, r_off_missing.output
    assert "existing mm- collections could not be removed" in r_off_missing.stderr
    assert config.load_config(env.cfg_path)["memory_index"] == off_with_embeddings
    assert {"mm-claude", "keep-me"} <= set(stub.state()["collections"])

    # If removal fails, off remains saved and the same command can retry cleanup.
    r_on_again = runner.invoke(cli.app, ["memory-index", "on"])
    assert r_on_again.exit_code == 0, r_on_again.output
    stale_config = config.load_config(env.cfg_path)
    with monkeypatch.context() as mp:
        mp.setenv("QMD_STUB_FAIL", "collection remove")
        r_off_fail = runner.invoke(cli.app, ["memory-index", "off"])
    assert r_off_fail.exit_code == 1, r_off_fail.output
    assert "Run: mm memory-index off" in r_off_fail.stderr
    assert config.load_config(env.cfg_path)["memory_index"] == off_with_embeddings
    assert {"mm-claude", "keep-me"} <= set(stub.state()["collections"])

    r_off = runner.invoke(cli.app, ["memory-index", "off"])
    assert r_off.exit_code == 0, r_off.output
    reloaded2 = config.load_config(env.cfg_path)
    assert reloaded2["memory_index"] == off_with_embeddings
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
        codex_source = config.get_default_source("codex")
        assert codex_source is not None
        codex_source["path"] = str(codex_dir)
        cfg = {
            "device": {"id": did, "name": dname},
            "storage": {"path": str(storage_dir)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [codex_source],
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
    # Value: protects=sync outcomes survive qmd failure, and interrupted sync skips refresh;
    # fails_when=the refresh tail changes either output stream or runs on an interrupted sync;
    # why_new=the earlier CLI coverage only exercised missing qmd on push and pre-core refusal;
    # seam=none; no_claim=deterministic core outcomes isolate the CLI tail in output comparisons.
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

        with mp.context() as interrupt:

            def interrupt_after_sync(*_args, **_kwargs):
                events.append("interrupt")
                raise KeyboardInterrupt

            def spy_spawn(_cfg):
                events.append("spawn")
                return True

            interrupt.setattr(meshflow, "run", interrupt_after_sync)
            interrupt.setattr(memoryindex, "start_detached_refresh", spy_spawn)
            for command in ("push", "pull", "autopush", "autopull"):
                events.clear()
                interrupted = runner.invoke(cli.app, [command])
                assert interrupted.exit_code != 0, interrupted.output
                expected = ["interrupt"] if command.startswith("auto") else ["interrupt", "release"]
                assert events == expected, command

        events.clear()
        mp.setattr(pullflow, "_pull_core", lambda *a, **kw: pullflow.PullResult(total_failed=1))
        r_pull_failed = runner.invoke(cli.app, ["pull"])
        assert r_pull_failed.exit_code == 1, r_pull_failed.output
        assert events == ["release"]

    # Compare both streams and exit status against a healthy qmd refresh.
    fleet2 = _init_two_machine_env(tmp_path / "f2", monkeypatch)
    dev_a2 = fleet2.activate("aaaaaaaa")
    _make_qmd_stub(tmp_path / "sync-qmd", monkeypatch)
    empty_bin = tmp_path / "no-qmd-bin"
    empty_bin.mkdir()
    with monkeypatch.context() as mp:
        # Keep content outcomes stable while the real CLI invokes the qmd stub.
        mp.setattr(cli, "_push_core", lambda *_a, **_kw: cli.PushResult())
        mp.setattr(pullflow, "_pull_core", lambda *_a, **_kw: pullflow.PullResult())
        for command in ("push", "pull"):
            healthy = runner.invoke(cli.app, [command])
            assert healthy.exit_code == 0, healthy.output
            assert memoryindex.read_state()["result"] == "ok"
            expected = (healthy.exit_code, healthy.stdout, healthy.stderr)
            for failure in ("qmd-missing", "failed", "timed-out"):
                with mp.context() as condition:
                    if failure == "qmd-missing":
                        condition.setenv("PATH", str(empty_bin))
                    elif failure == "failed":
                        condition.setenv("QMD_STUB_FAIL", "update")
                    else:
                        condition.setattr(memoryindex, "REFRESH_DEADLINE_S", 0.3)
                        condition.setenv("QMD_STUB_SLEEP", "1.0")
                    result = runner.invoke(cli.app, [command])
                assert (result.exit_code, result.stdout, result.stderr) == expected, failure
                assert memoryindex.read_state()["result"] == failure

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
        assert r_apull_failed.exit_code == 0, r_apull_failed.output
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
    # Value: protects=diagnostics honor the toggle and retain refresh evidence if qmd is missing;
    # fails_when=disabled status warns about stale history or enabled diag drops it;
    # why_new=the earlier cases exercise healthy enabled diag and an off device with no history;
    # seam=none; no_claim=isolated fixtures model qmd availability and local record data.
    stub = _make_qmd_stub(tmp_path, monkeypatch)

    # Off ignores stale failure in status and preserves it in diag without invoking qmd.
    env_off = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="dg000001",
        enabled=False,
        sources=[],
    )
    backend = local_storage.LocalBackend(env_off.storage_dir)
    boot = crypto.bootstrap_crypto_init(backend, PASSPHRASE, MEMORY_KB)
    crypto.set_crypto_session(boot.root_salt, MEMORY_KB)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    devices.register_device(backend, "dg000001", "Mac A")
    previous_at = "2026-11-21T12:00:00+00:00"
    memoryindex.state_path().write_text(
        json.dumps(
            {
                "version": 1,
                "result": "failed",
                "at": previous_at,
                "qmd_version": "2.8.3",
                "collections": ["mm-claude"],
            }
        ),
        encoding="utf-8",
    )
    status = runner.invoke(cli.app, ["status"])
    assert status.exit_code == 0, status.output
    assert "Memory index:" not in status.output
    r_off = runner.invoke(cli.app, ["diag", "--json"])
    assert r_off.exit_code == 0, r_off.output
    data_off = json.loads(r_off.output)
    assert data_off["memory_index"] == {
        "enabled": False,
        "embeddings": False,
        "qmd_version": None,
        "collections": [],
        "last_refresh": {"state": "recorded", "result": "failed", "at": previous_at},
    }
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
    refresh = memoryindex.refresh(env_on.cfg)
    assert refresh.status == "ok"
    r_on = runner.invoke(cli.app, ["diag", "--json"])
    assert r_on.exit_code == 0, r_on.output
    data_on = json.loads(r_on.output)
    mi = data_on["memory_index"]
    assert mi == {
        "enabled": True,
        "embeddings": False,
        "qmd_version": "2.8.3",
        "collections": ["mm-claude"],
        "last_refresh": {"state": "recorded", "result": "ok", "at": refresh.at},
    }

    empty_bin = tmp_path / "diag-no-qmd"
    empty_bin.mkdir()
    with monkeypatch.context() as mp:
        mp.setenv("PATH", str(empty_bin))
        unavailable = runner.invoke(cli.app, ["diag", "--json"])
    assert unavailable.exit_code == 0, unavailable.output
    assert json.loads(unavailable.output)["memory_index"] == {
        **mi,
        "qmd_version": None,
        "collections": [],
    }

    # Invalid collection names in the local record cannot become qmd arguments.
    state_path = memoryindex.state_path()
    record = json.loads(state_path.read_text())
    record["collections"] = ["mm-invalid name"]
    state_path.write_text(json.dumps(record), encoding="utf-8")
    invalid = runner.invoke(cli.app, ["diag", "--json"])
    assert invalid.exit_code == 0, invalid.output
    invalid_state = json.loads(invalid.output)["memory_index"]
    assert invalid_state["last_refresh"]["state"] == "malformed"
    assert invalid_state["collections"] == []
    assert stub.calls()[-1] == ["--version"]

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
    for cmd in (
        "claude mcp add mind-meld -- qmd --index mind-meld mcp",
        "codex mcp add mind-meld -- qmd --index mind-meld mcp",
    ):
        assert cmd in readme
        for py in (root / "src").rglob("*.py"):
            content = py.read_text(encoding="utf-8")
            assert cmd not in content, f"{py} must not contain {cmd!r}"
            assert "claude mcp add" not in content, f"{py} must not contain 'claude mcp add'"
            assert "codex mcp add" not in content, f"{py} must not contain 'codex mcp add'"


# ── Track 162A: Old/missing qmd hint, malformed qmd output, off lock retry ───


def test_memory_index_on_old_version_prints_runtime_install_hint_and_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _make_qmd_stub(tmp_path, monkeypatch, version="qmd 2.8.2")
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects" / "p" / "memory").mkdir(parents=True)
    user_dir = tmp_path / "user-docs"
    user_dir.mkdir()
    stub.seed_collection("keep-me", user_dir)
    stub.seed_collection("mm-claude", claude_dir, mask="*/memory/**/*.md")

    env = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="old162a1",
        enabled=False,
        embeddings=True,
        sources=[{"name": "claude", "path": str(claude_dir), "type": "claude"}],
    )

    cfg_bytes = env.cfg_path.read_bytes()
    cols_before = stub.state()["collections"]

    # Bun branch: fake bun on isolated PATH yields the bun install command.
    bun_dir = tmp_path / "bun-bin"
    bun_dir.mkdir()
    fake_bun = bun_dir / "bun"
    fake_bun.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bun.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub.bin_dir}{os.pathsep}{bun_dir}")

    r_bun = runner.invoke(cli.app, ["memory-index", "on"])
    assert r_bun.exit_code == 1
    assert r_bun.stdout == ""
    assert r_bun.stderr.splitlines()[0] == "mm: error: could not verify qmd 2.8.3+ (found 2.8.2)."
    assert r_bun.stderr.splitlines()[-1] == "Run: bun install -g @tobilu/qmd"
    assert env.cfg_path.read_bytes() == cfg_bytes
    assert config.load_config(env.cfg_path)["memory_index"] == {
        "enabled": False,
        "embeddings": True,
    }
    assert not memoryindex.state_path().exists()
    assert stub.state()["collections"] == cols_before
    assert stub.calls() == [["--version"]]

    # npm branch: strict PATH without bun yields the npm prefix install command.
    monkeypatch.setenv("PATH", str(stub.bin_dir))
    before_npm = len(stub.calls())
    r_npm = runner.invoke(cli.app, ["memory-index", "on"])
    expected_npm_hint = (
        f"npm install -g --prefix {shlex.quote(str(env.home / '.local'))} @tobilu/qmd"
    )
    assert memoryindex.install_hint() == expected_npm_hint
    assert r_npm.exit_code == 1
    assert r_npm.stdout == ""
    assert r_npm.stderr.splitlines()[0] == "mm: error: could not verify qmd 2.8.3+ (found 2.8.2)."
    assert r_npm.stderr.splitlines()[-1] == f"Run: {expected_npm_hint}"
    assert env.cfg_path.read_bytes() == cfg_bytes
    assert not memoryindex.state_path().exists()
    assert stub.state()["collections"] == cols_before
    assert stub.calls()[before_npm:] == [["--version"]]

    # Bare read-only inspection with old qmd exits 0, shows minimum, and writes nothing.
    before_bare = len(stub.calls())
    stub_bytes = stub.state_path.read_bytes()
    r_bare = runner.invoke(cli.app, ["memory-index"])
    assert r_bare.exit_code == 0, r_bare.output
    assert "2.8.2 (requires 2.8.3+)" in r_bare.output
    assert env.cfg_path.read_bytes() == cfg_bytes
    assert not memoryindex.state_path().exists()
    assert stub.state_path.read_bytes() == stub_bytes
    assert stub.calls()[before_bare:] == [["--version"]]


def test_refresh_rejects_malformed_zero_exit_collection_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _make_qmd_stub(tmp_path, monkeypatch)
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects" / "p" / "memory").mkdir(parents=True)
    (claude_dir / "projects" / "p" / "memory" / "MEMORY.md").write_text(
        "# Note\nhello\n", encoding="utf-8"
    )
    user_dir = tmp_path / "personal-notes"
    user_dir.mkdir()
    stale_dir = tmp_path / "stale-claude"
    stale_dir.mkdir()
    stub.seed_collection("personal-notes", user_dir)

    env = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="malf162a",
        enabled=True,
        embeddings=False,
        sources=[{"name": "claude", "path": str(claude_dir), "type": "claude"}],
    )

    # Establish a prior recorded "ok" state in memory-index.json.
    init_res = memoryindex.refresh(env.cfg)
    assert init_res.status == "ok"
    assert memoryindex.read_state()["result"] == "ok"
    ok_state_bytes = memoryindex.state_path().read_bytes()

    # Point mm-claude at a stale directory so any fallthrough to reconciliation mutates stub state.
    stub.seed_collection("mm-claude", stale_dir, mask="*/memory/**/*.md")
    expected_collections = stub.state()["collections"]

    malformed_list_payloads = [
        "Collections:\n  mm-claude  (qmd://mm-claude/)\n",
        "Collections (2):\n  mm-claude  (qmd://mm-claude/)\n",
        "",
    ]
    for payload in malformed_list_payloads:
        memoryindex.state_path().write_bytes(ok_state_bytes)
        assert memoryindex.read_state()["result"] == "ok"
        before = len(stub.calls())
        with monkeypatch.context() as mp:
            mp.setenv("QMD_STUB_LIST_STDOUT", payload)
            res = memoryindex.refresh(env.cfg)
        assert res.status == "failed"
        assert memoryindex.read_state()["result"] == "failed"
        assert stub.calls()[before:] == [["--version"], ["collection", "list"]]
        assert stub.state()["collections"] == expected_collections

    malformed_show_payloads = [
        "Collection: mm-claude\n  Pattern: */memory/**/*.md\n",
        "Collection: mm-claude\n  Path: /tmp/old-projects\n",
    ]
    for payload in malformed_show_payloads:
        memoryindex.state_path().write_bytes(ok_state_bytes)
        assert memoryindex.read_state()["result"] == "ok"
        before = len(stub.calls())
        with monkeypatch.context() as mp:
            mp.setenv("QMD_STUB_SHOW_STDOUT", payload)
            res = memoryindex.refresh(env.cfg)
        assert res.status == "failed"
        assert memoryindex.read_state()["result"] == "failed"
        assert stub.calls()[before:] == [
            ["--version"],
            ["collection", "list"],
            ["collection", "show", "mm-claude"],
        ]
        assert stub.state()["collections"] == expected_collections


def test_memory_index_off_waits_for_active_refresh_and_removes_collections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _make_qmd_stub(tmp_path, monkeypatch)
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects" / "p" / "memory").mkdir(parents=True)
    (claude_dir / "projects" / "p" / "memory" / "MEMORY.md").write_text(
        "# Note\nhello\n", encoding="utf-8"
    )
    user_dir = tmp_path / "user-docs"
    user_dir.mkdir()
    stub.seed_collection("keep-me", user_dir)

    env = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="offw162a",
        enabled=True,
        embeddings=False,
        sources=[{"name": "claude", "path": str(claude_dir), "type": "claude"}],
    )

    refresh_holding_lock = threading.Event()
    allow_refresh_finish = threading.Event()
    refresh_finished = threading.Event()
    real_run_qmd = memoryindex._run_qmd

    def paused_run_qmd(qmd, args, deadline, *, inspect=False):
        if list(args) == ["update"]:
            refresh_holding_lock.set()
            assert allow_refresh_finish.wait(timeout=10.0)
        return real_run_qmd(qmd, args, deadline, inspect=inspect)

    monkeypatch.setattr(memoryindex, "_run_qmd", paused_run_qmd)

    refresh_results: list[memoryindex.RefreshResult] = []
    worker_errors: list[BaseException] = []

    def run_refresh() -> None:
        try:
            refresh_results.append(memoryindex.refresh(env.cfg))
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            refresh_finished.set()

    thread = threading.Thread(target=run_refresh)
    thread.start()
    retry_delays: list[float] = []
    try:
        assert refresh_holding_lock.wait(timeout=10.0)

        def release_on_first_retry(delay: float) -> None:
            retry_delays.append(delay)
            allow_refresh_finish.set()
            assert refresh_finished.wait(timeout=10.0)

        monkeypatch.setattr(lockedjson, "time", SimpleNamespace(sleep=release_on_first_retry))
        r_off = runner.invoke(cli.app, ["memory-index", "off"])
    finally:
        allow_refresh_finish.set()
        thread.join(timeout=10.0)

    assert not thread.is_alive()
    assert not worker_errors
    assert len(refresh_results) == 1
    assert refresh_results[0].status == "ok"
    assert retry_delays == [0.1]
    assert r_off.exit_code == 0, r_off.output
    assert "Memory-Index: disabled on this device (1 collection removed)." in r_off.output
    assert config.load_config(env.cfg_path)["memory_index"] == {
        "enabled": False,
        "embeddings": False,
    }
    assert "mm-claude" not in stub.state()["collections"]
    assert "keep-me" in stub.state()["collections"]
    assert memoryindex.read_state()["state"] == "missing"


# ── Track 163A: Dedicated mind-meld index, migration, preflight, setup link ──


def test_named_index_migration_isolation_retry_and_fresh_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _make_qmd_stub(tmp_path, monkeypatch)

    # Create a project directory with its own .qmd/index.yml and chdir into it.
    project_dir = tmp_path / "project-repo"
    (project_dir / ".qmd").mkdir(parents=True)
    proj_yml = project_dir / ".qmd" / "index.yml"
    proj_yml.write_text("collections: {}\n", encoding="utf-8")
    proj_index_key = f"project:{proj_yml.resolve()}"
    proj_notes_dir = project_dir / "docs"
    proj_notes_dir.mkdir()
    (proj_notes_dir / "spec.md").write_text("project-only-fact-163a\n", encoding="utf-8")
    stub.seed_collection(
        "proj-notes",
        proj_notes_dir,
        index=proj_index_key,
        docs={"spec.md": "project-only-fact-163a\n"},
    )

    # Seed default `index` with a user `notes` collection plus legacy `mm-` collections.
    notes_dir = tmp_path / "default-notes"
    notes_dir.mkdir()
    (notes_dir / "journal.md").write_text("default-index-notes-fact\n", encoding="utf-8")
    stub.seed_collection(
        "notes",
        notes_dir,
        index="index",
        docs={"journal.md": "default-index-notes-fact\n"},
    )

    claude_dir = tmp_path / "claude"
    (claude_dir / "projects" / "p" / "memory").mkdir(parents=True)
    (claude_dir / "projects" / "p" / "memory" / "note.md").write_text(
        "mind-meld-claude-fact-163a\n", encoding="utf-8"
    )
    grok_dir = tmp_path / "grok"
    (grok_dir / "memory-v2" / "global" / "topics").mkdir(parents=True)
    (grok_dir / "memory-v2" / "global" / "topics" / "t.md").write_text(
        "mind-meld-grok-fact-163a\n", encoding="utf-8"
    )

    stub.seed_collection("mm-claude", claude_dir / "projects", "*/memory/**/*.md", index="index")
    stub.seed_collection("mm-grok", grok_dir / "memory-v2", "**/topics/**/*.md", index="index")

    env = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="mig163a1",
        enabled=False,
        embeddings=True,
        sources=[
            {"name": "claude", "path": str(claude_dir), "type": "claude"},
            {"name": "grok", "path": str(grok_dir), "type": "grok"},
        ],
    )
    monkeypatch.chdir(project_dir)

    # Write a legacy v1 memory-index.json (no "index" field).
    legacy_record = {
        "version": 1,
        "result": "ok",
        "at": "2026-11-21T12:00:00+00:00",
        "qmd_version": "2.8.3",
        "collections": ["mm-claude", "mm-grok"],
    }
    memoryindex.state_path().write_text(json.dumps(legacy_record), encoding="utf-8")
    assert memoryindex.read_state()["index"] == "index"

    # Invalid "index" values validate as malformed.
    for bad_index in ("other", 123, None, ""):
        assert memoryindex._validate_state_record({**legacy_record, "index": bad_index}) is None

    # Migration on `mm memory-index on`: creates/updates mind-meld, removes only legacy mm-
    # collections from default `index`, never updates/embeds default or project index.
    popen_calls: list[list[str]] = []
    spawned_embeds: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def spy_popen(cmd, **kwargs):
        cmd_list = list(cmd)
        popen_calls.append(cmd_list)
        proc = real_popen(cmd, **kwargs)
        if cmd_list[-1:] == ["embed"]:
            spawned_embeds.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    r_on = runner.invoke(cli.app, ["memory-index", "on"])
    monkeypatch.setattr(subprocess, "Popen", real_popen)
    for proc in spawned_embeds:
        proc.wait(timeout=5.0)
    spawned_embeds.clear()

    assert r_on.exit_code == 0, r_on.output
    assert set(stub.state("mind-meld")["collections"]) == {"mm-claude", "mm-grok"}
    assert set(stub.state("index")["collections"]) == {"notes"}
    assert set(stub.state(proj_index_key)["collections"]) == {"proj-notes"}

    # Zero update/embed calls on default or project index; embed ran on mind-meld.
    assert [c for c in popen_calls if c[1:] == ["--index", "mind-meld", "embed"]]
    for entry in stub.indexed_calls():
        if entry["cmd"] and entry["cmd"][0] in ("update", "embed"):
            assert entry["index"] == "mind-meld"
        assert entry["index"] != proj_index_key

    stored = json.loads(memoryindex.state_path().read_text(encoding="utf-8"))
    assert stored["index"] == "mind-meld"
    assert stored["result"] == "ok"
    assert stored["collections"] == ["mm-claude", "mm-grok"]

    # Search isolation across mind-meld, default index, and project-local index.
    assert "qmd://mm-claude/p/memory/note.md" in stub.search(
        "mind-meld-claude-fact-163a", index="mind-meld", cwd=project_dir
    )
    assert "No results found." in stub.search(
        "default-index-notes-fact", index="mind-meld", cwd=project_dir
    )
    assert "qmd://notes/journal.md" in stub.search(
        "default-index-notes-fact", index="index", cwd=project_dir
    )
    assert "No results found." in stub.search(
        "mind-meld-claude-fact-163a", index="index", cwd=project_dir
    )
    assert "qmd://proj-notes/spec.md" in stub.search(
        "project-only-fact-163a", index=None, cwd=project_dir
    )
    assert "No results found." in stub.search(
        "mind-meld-claude-fact-163a", index=None, cwd=project_dir
    )

    # Routine refresh after migration makes zero calls to default or project index.
    enabled_cfg = config.load_config(env.cfg_path)
    before_routine = len(stub.indexed_calls())
    with monkeypatch.context() as mp:
        mp.setattr(subprocess, "Popen", spy_popen)
        assert memoryindex.refresh(enabled_cfg).status == "ok"
    for proc in spawned_embeds:
        proc.wait(timeout=5.0)
    spawned_embeds.clear()
    routine_calls = stub.indexed_calls()[before_routine:]
    assert all(e["index"] in (None, "mind-meld") for e in routine_calls)

    # Write-free bare `mm memory-index` and `mm diag --json` inspect only `--index mind-meld`.
    observed_files = (env.cfg_path, memoryindex.state_path(), stub.state_path)
    before_inspect = [p.read_bytes() for p in observed_files]
    before_inspect_calls = len(stub.indexed_calls())
    r_bare = runner.invoke(cli.app, ["memory-index"])
    assert r_bare.exit_code == 0, r_bare.output
    assert "mm-claude" in r_bare.output
    assert "notes" not in r_bare.output
    r_diag = runner.invoke(cli.app, ["diag", "--json"])
    assert r_diag.exit_code == 0, r_diag.output
    diag_view = json.loads(r_diag.output)["memory_index"]
    assert set(diag_view["last_refresh"]) == {"state", "result", "at"}
    assert diag_view["collections"] == ["mm-claude", "mm-grok"]
    assert [p.read_bytes() for p in observed_files] == before_inspect
    inspect_calls = stub.indexed_calls()[before_inspect_calls:]
    assert all(e["index"] in (None, "mind-meld") for e in inspect_calls)
    assert all(e["cmd"][0] in ("--version", "collection") for e in inspect_calls)

    # Partial cleanup failure during enabled refresh keeps index="index", defers embed,
    # retains reconciled mind-meld collections, and retries cleanly on next refresh.
    stub.seed_collection("mm-claude", claude_dir / "projects", "*/memory/**/*.md", index="index")
    stub.seed_collection("mm-grok", grok_dir / "memory-v2", "**/topics/**/*.md", index="index")
    memoryindex.state_path().write_text(json.dumps(legacy_record), encoding="utf-8")

    popen_partial: list[list[str]] = []

    def spy_partial(cmd, **kwargs):
        popen_partial.append(list(cmd))
        return real_popen(cmd, **kwargs)

    with monkeypatch.context() as mp:
        mp.setenv("QMD_STUB_FAIL", "index:collection remove:mm-grok")
        mp.setattr(subprocess, "Popen", spy_partial)
        res_partial = memoryindex.refresh(enabled_cfg)
    assert res_partial.status == "failed"
    assert not any(c[1:] == ["--index", "mind-meld", "embed"] for c in popen_partial)
    partial_stored = json.loads(memoryindex.state_path().read_text(encoding="utf-8"))
    assert partial_stored["index"] == "index"
    assert partial_stored["result"] == "failed"
    assert partial_stored["collections"] == ["mm-claude", "mm-grok"]
    assert set(stub.state("index")["collections"]) == {"notes", "mm-grok"}

    popen_retry: list[list[str]] = []

    def spy_retry(cmd, **kwargs):
        popen_retry.append(list(cmd))
        return real_popen(cmd, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(subprocess, "Popen", spy_retry)
        res_retry = memoryindex.refresh(enabled_cfg)
    assert res_retry.status == "ok"
    assert any(c[1:] == ["--index", "mind-meld", "embed"] for c in popen_retry)
    retry_stored = json.loads(memoryindex.state_path().read_text(encoding="utf-8"))
    assert retry_stored["index"] == "mind-meld"
    assert retry_stored["result"] == "ok"
    assert set(stub.state("index")["collections"]) == {"notes"}

    # `mm memory-index off` removes only mm- collections from mind-meld;
    # default and project indexes stay intact.
    r_off = runner.invoke(cli.app, ["memory-index", "off"])
    assert r_off.exit_code == 0, r_off.output
    assert stub.state("mind-meld")["collections"] == {}
    assert set(stub.state("index")["collections"]) == {"notes"}
    assert set(stub.state(proj_index_key)["collections"]) == {"proj-notes"}

    # Fresh installation (no prior memory-index.json) makes zero calls to default or project index.
    memoryindex.state_path().unlink(missing_ok=True)
    before_fresh = len(stub.indexed_calls())
    r_fresh = runner.invoke(cli.app, ["memory-index", "on"])
    assert r_fresh.exit_code == 0, r_fresh.output
    fresh_calls = stub.indexed_calls()[before_fresh:]
    assert all(e["index"] in (None, "mind-meld") for e in fresh_calls)
    assert set(stub.state("index")["collections"]) == {"notes"}
    assert set(stub.state(proj_index_key)["collections"]) == {"proj-notes"}


def test_memory_index_on_installed_qmd_preflight_failures_preserve_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _make_qmd_stub(tmp_path, monkeypatch)
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects" / "p" / "memory").mkdir(parents=True)
    user_dir = tmp_path / "user-docs"
    user_dir.mkdir()
    stub.seed_collection("keep-me", user_dir, index="mind-meld")
    stub.seed_collection("notes", user_dir, index="index")

    env = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="pre163a1",
        enabled=False,
        embeddings=True,
        sources=[{"name": "claude", "path": str(claude_dir), "type": "claude"}],
    )

    prior_record_bytes = json.dumps(
        {
            "version": 1,
            "result": "ok",
            "at": "2026-11-21T12:00:00+00:00",
            "qmd_version": "2.8.3",
            "collections": ["mm-claude"],
        }
    ).encode("utf-8")

    cfg_before = env.cfg_path.read_bytes()
    stub_before = stub.state_path.read_bytes()
    expected_hint = memoryindex.install_hint()

    for with_existing_state in (False, True):
        if with_existing_state:
            memoryindex.state_path().write_bytes(prior_record_bytes)
        else:
            memoryindex.state_path().unlink(missing_ok=True)

        # (a) Nonzero exit on --version
        before = len(stub.calls())
        with monkeypatch.context() as mp:
            mp.setenv("QMD_STUB_FAIL", "version")
            res = runner.invoke(cli.app, ["memory-index", "on"])
        assert res.exit_code == 1
        assert res.stdout == ""
        assert res.stderr.splitlines()[0] == "mm: error: could not verify qmd 2.8.3+ (failed)."
        assert res.stderr.splitlines()[-1] == f"Run: {expected_hint}"
        assert env.cfg_path.read_bytes() == cfg_before
        assert stub.state_path.read_bytes() == stub_before
        assert stub.calls()[before:] == [["--version"]]
        if with_existing_state:
            assert memoryindex.state_path().read_bytes() == prior_record_bytes
        else:
            assert not memoryindex.state_path().exists()

        # (b) Exit 0 with unparseable stdout
        for bad_stdout in (
            "qmd dev-snapshot",
            "qmd dev-snapshot (Node 26.13.1)",
            "qmd 2.8.3broken",
            "",
        ):
            before = len(stub.calls())
            with monkeypatch.context() as mp:
                mp.setenv("QMD_STUB_VERSION", bad_stdout)
                res = runner.invoke(cli.app, ["memory-index", "on"])
            assert res.exit_code == 1
            assert res.stdout == ""
            assert res.stderr.splitlines()[0] == "mm: error: could not verify qmd 2.8.3+ (failed)."
            assert res.stderr.splitlines()[-1] == f"Run: {expected_hint}"
            assert env.cfg_path.read_bytes() == cfg_before
            assert stub.state_path.read_bytes() == stub_before
            assert stub.calls()[before:] == [["--version"]]
            if with_existing_state:
                assert memoryindex.state_path().read_bytes() == prior_record_bytes
            else:
                assert not memoryindex.state_path().exists()

        # (c) Timeout on --version (deterministic Popen stub + SIGKILL assertion)
        attempted_argvs: list[list[str]] = []
        killed: list[tuple[object, int]] = []
        sentinel_pid = object()

        def timeout_popen(argv, **_kwargs):
            attempted_argvs.append(list(argv))

            def communicate(*, timeout):
                raise subprocess.TimeoutExpired(argv, timeout)

            return nullcontext(
                SimpleNamespace(pid=sentinel_pid, communicate=communicate, returncode=None)
            )

        def record_killpg(pgid, sig):
            killed.append((pgid, sig))

        with monkeypatch.context() as mp:
            mp.setattr(subprocess, "Popen", timeout_popen)
            mp.setattr(os, "killpg", record_killpg)
            res = runner.invoke(cli.app, ["memory-index", "on"])
        assert res.exit_code == 1
        assert res.stdout == ""
        assert res.stderr.splitlines()[0] == "mm: error: could not verify qmd 2.8.3+ (timed-out)."
        assert res.stderr.splitlines()[-1] == f"Run: {expected_hint}"
        assert attempted_argvs == [[str(stub.script), "--version"]]
        assert killed == [(sentinel_pid, signal.SIGKILL)]
        assert env.cfg_path.read_bytes() == cfg_before
        assert stub.state_path.read_bytes() == stub_before
        if with_existing_state:
            assert memoryindex.state_path().read_bytes() == prior_record_bytes
        else:
            assert not memoryindex.state_path().exists()


def test_memory_index_on_emits_agent_setup_link_and_preserves_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_qmd_stub(tmp_path, monkeypatch)
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects" / "p" / "memory").mkdir(parents=True)

    env = _setup_device_env(
        tmp_path,
        monkeypatch,
        device_id="lnk163a1",
        enabled=False,
        embeddings=False,
        sources=[{"name": "claude", "path": str(claude_dir), "type": "claude"}],
    )

    config_repo = tmp_path / "dotfiles"
    config_repo.mkdir()
    claude_target = config_repo / "CLAUDE.md"
    codex_target = config_repo / "AGENTS.md"
    claude_target.write_text("# Claude instructions\n", encoding="utf-8")
    codex_target.write_text("# Codex instructions\n", encoding="utf-8")
    (env.home / ".claude").mkdir(exist_ok=True)
    (env.home / ".codex").mkdir(exist_ok=True)
    claude_link = env.home / ".claude" / "CLAUDE.md"
    codex_link = env.home / ".codex" / "AGENTS.md"
    claude_link.symlink_to(claude_target)
    codex_link.symlink_to(codex_target)

    r_on = runner.invoke(cli.app, ["memory-index", "on"])
    assert r_on.exit_code == 0, r_on.output
    assert "Memory-Index:" in r_on.output
    assert memoryindex.AGENT_SETUP_URL in r_on.output
    assert "Run: mm memory-index off" in r_on.output
    assert claude_link.is_symlink()
    assert codex_link.is_symlink()
    assert claude_target.read_text(encoding="utf-8") == "# Claude instructions\n"
    assert codex_target.read_text(encoding="utf-8") == "# Codex instructions\n"

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert "\n## Agent setup\n" in readme
