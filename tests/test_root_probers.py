"""Track 102A: automatic git-root discovery from host records.

Every test builds a ``tmp_path`` home with synthetic records. The autouse
``_isolate_git_capture_cache`` fixture redirects ``rootprobes._discovery_home``
so none of these ever reads the real account's Codex, Cursor, Muse,
Antigravity, Paseo or mmm stores.
"""

from __future__ import annotations

import builtins
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

from mind_meld import config as mm_config
from mind_meld import events, events_tail, rootprobes, token_usage
from tests.test_events import _git_env_repos, _init_git_repo, _make_git_worktree

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A synthetic account home for discovery and for ``Path.home()``."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    monkeypatch.setattr(rootprobes, "_discovery_home", lambda: root)
    return root


def _fake_repo(path: Path) -> Path:
    """A directory discovery classifies as a git root, without spawning git."""
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path.resolve()


def _cfg(*sources: str, cursor: bool = False, disabled: tuple[str, ...] = ()) -> dict:
    config: dict = {"sync": {"sources": [{"name": name} for name in sources]}}
    if disabled:
        config["sync"]["disabled_sources"] = list(disabled)
    if cursor:
        config["retro"] = {"cursor_host_usage": True}
    return config


def _discover(config: dict, *, persist: bool = False, seconds: float = 30.0):
    return events.discover_git_roots(
        config, deadline_monotonic=time.monotonic() + seconds, persist=persist
    )


def _cached_units(name: str) -> dict:
    return events.read_git_capture_cache()["probe_index"][name]["units"]


def _codex_rollout(
    path: Path,
    cwd: str,
    *,
    before_cwd: int = 0,
    after_cwd: int = 0,
    first_type: str = "session_meta",
) -> Path:
    """One rollout: a compact first record shaped like Codex's ``session_meta``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {"id": "abc"}
    if before_cwd:
        payload["instructions"] = "x" * before_cwd
    payload["cwd"] = cwd
    payload["git"] = {"branch": "main"}
    if after_cwd:
        payload["tail"] = "y" * after_cwd
    first = json.dumps(
        {"timestamp": "2026-11-03T10:00:00Z", "ordinal": 0, "type": first_type, "payload": payload},
        separators=(",", ":"),
    )
    path.write_text(first + "\n" + json.dumps({"type": "event_msg"}) + "\n")
    return path


def _codex_day(home: Path, day: str = "2026/11/03") -> Path:
    return home / ".codex" / "sessions" / day


def _bump(path: Path, offset_ns: int) -> None:
    """Give a directory a distinct mtime: coarse filesystem clocks tie otherwise."""
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + offset_ns))


def _make_db(path: Path, table: str, column: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(f"create table {table} ({column} text)")
    connection.executemany(f"insert into {table} values (?)", [(row,) for row in rows])
    connection.commit()
    connection.close()


def _synthetic(units: dict, *, stamps: dict | None = None, reads: list | None = None, clock=None):
    """A ``RootProber`` named codex over ``units`` (key -> directories, or None)."""
    reads = reads if reads is not None else []

    def list_units(_home, _state, _deadline):
        return [(key, (stamps or {}).get(key)) for key in list(units)]

    def read_unit(_home, key, _deadline):
        reads.append(key)
        if clock is not None:
            clock[0] += 1.0
        value = units[key]
        return (None, True) if value is None else (list(value), True)

    return rootprobes.RootProber("codex", "provider", list_units, read_unit)


# ---------------------------------------------------------------------------
# T2: the generic indexed prober, registry, cache and test isolation
# ---------------------------------------------------------------------------


class TestRegistryAndIsolation:
    def test_registry_order_puts_cheap_tools_first(self):
        assert [p.name for p in rootprobes.PROBERS] == [
            "paseo",
            "mmm",
            "agy",
            "muse",
            "cursor",
            "codex",
        ]
        assert {p.name: p.kind for p in rootprobes.PROBERS} == {
            "paseo": "tool",
            "mmm": "tool",
            "agy": "provider",
            "muse": "provider",
            "cursor": "provider",
            "codex": "provider",
        }

    def test_discovery_home_is_not_the_real_home_by_default(self):
        real = Path(pwd.getpwuid(os.getuid()).pw_dir)
        assert rootprobes._discovery_home() != real
        assert rootprobes._discovery_home().name == "_isolated_discovery_home"

    def test_probers_refuse_the_real_home_under_pytest(self):
        real = Path(pwd.getpwuid(os.getuid()).pw_dir)
        for prober in rootprobes.PROBERS:
            with pytest.raises(RuntimeError, match="real account"):
                prober.list_units(real, {}, None)

    def test_corpus_larger_than_the_deadline_completes_over_pushes(self, home, monkeypatch):
        repos = {f"u{i}": [str(_fake_repo(home / f"repo{i}"))] for i in range(10)}
        reads: list[str] = []
        clock = [1000.0]
        monkeypatch.setattr(events.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(rootprobes, "PROBERS", (_synthetic(repos, reads=reads, clock=clock),))

        def push():
            return events.discover_git_roots(
                _cfg("codex"), deadline_monotonic=clock[0] + 3.5, persist=True
            )

        first = push()
        assert first.exceeded and len(first.roots) == 3 and len(reads) == 4
        second = push()
        assert second.exceeded and len(second.roots) == 6 and len(reads) == 8
        third = push()
        # The third push finishes its round and, with budget left, closes it in
        # the same call by replaying the one unit the second push interrupted
        # (Track 106A), so it holds nine roots, not eight.
        assert third.exceeded and len(third.roots) == 9
        fourth = push()
        assert not fourth.exceeded and len(fourth.roots) == 10
        assert fourth.attribution and {source for source, _ in fourth.attribution} == {"codex"}
        reads.clear()
        assert not push().exceeded
        assert reads == [], "a warm index reads no unit"

    def test_stamp_change_rereads_only_that_unit(self, home, monkeypatch):
        units = {
            "a": [str(_fake_repo(home / "a"))],
            "b": [str(_fake_repo(home / "b"))],
        }
        stamps = {"a": [1], "b": [1]}
        reads: list[str] = []
        monkeypatch.setattr(rootprobes, "PROBERS", (_synthetic(units, stamps=stamps, reads=reads),))
        _discover(_cfg("codex"), persist=True)
        assert sorted(reads) == ["a", "b"]
        reads.clear()
        stamps["a"] = [2]
        units["a"] = [str(_fake_repo(home / "a2"))]
        found = _discover(_cfg("codex"), persist=True)
        assert reads == ["a"]
        assert {p.name for p in found.roots} == {"a2", "b"}

    def test_vanished_unit_drops_its_roots(self, home, monkeypatch):
        units = {
            "a": [str(_fake_repo(home / "a"))],
            "b": [str(_fake_repo(home / "b"))],
        }
        monkeypatch.setattr(rootprobes, "PROBERS", (_synthetic(units),))
        assert len(_discover(_cfg("codex"), persist=True).roots) == 2
        del units["b"]
        found = _discover(_cfg("codex"), persist=True)
        assert [p.name for p in found.roots] == ["a"]
        assert set(_cached_units("codex")) == {"a"}

    def test_incomplete_listing_keeps_cached_roots_and_prunes_nothing(self, home, monkeypatch):
        units = {"a": [str(_fake_repo(home / "a"))]}
        prober = _synthetic(units)
        monkeypatch.setattr(rootprobes, "PROBERS", (prober,))
        _discover(_cfg("codex"), persist=True)
        cut = rootprobes.RootProber("codex", "provider", lambda *_: None, prober.read_unit)
        monkeypatch.setattr(rootprobes, "PROBERS", (cut,))
        found = _discover(_cfg("codex"), persist=True)
        assert found.exceeded
        assert [p.name for p in found.roots] == ["a"]
        assert set(_cached_units("codex")) == {"a"}

    def test_persist_false_creates_no_cache(self, home, monkeypatch):
        monkeypatch.setattr(
            rootprobes, "PROBERS", (_synthetic({"a": [str(_fake_repo(home / "a"))]}),)
        )
        found = _discover(_cfg("codex"), persist=False)
        assert [p.name for p in found.roots] == ["a"]
        assert not mm_config.GIT_CAPTURE_CACHE_PATH.exists()

    def test_deselected_provider_loses_its_index_on_complete_discovery(self, home, monkeypatch):
        monkeypatch.setattr(
            rootprobes, "PROBERS", (_synthetic({"a": [str(_fake_repo(home / "a"))]}),)
        )
        assert _discover(_cfg("codex"), persist=True).probers_ran == ("codex",)
        assert "codex" in events.read_git_capture_cache()["probe_index"]
        off = _discover(_cfg("codex", disabled=("codex",)), persist=True)
        assert off.probers_ran == () and not off.exceeded
        assert "codex" not in events.read_git_capture_cache()["probe_index"]

    def test_cached_root_that_stopped_being_a_repository_is_dropped(self, home, monkeypatch):
        repo = _fake_repo(home / "gone")
        keeper = _fake_repo(home / "keeper")
        monkeypatch.setattr(
            rootprobes,
            "PROBERS",
            (_synthetic({"a": [str(repo)], "b": [str(keeper)]}),),
        )
        assert len(_discover(_cfg("codex"), persist=True).roots) == 2
        shutil.rmtree(repo / ".git")
        found = _discover(_cfg("codex"), persist=True)
        assert [p.name for p in found.roots] == ["keeper"]
        assert sum(n for _reason, n in found.reject_counts) == 1
        units = _cached_units("codex")
        assert units["a"][1] == [] and units["b"][1] == [str(keeper)]

    def test_named_directories_are_classified_once_and_only_roots_are_stored(
        self, home, monkeypatch
    ):
        repo = _fake_repo(home / "repo")
        plain = home / "plain"
        plain.mkdir()
        units = {"a": [str(plain), "relative/dir", str(repo), str(repo), ""]}
        monkeypatch.setattr(rootprobes, "PROBERS", (_synthetic(units),))
        found = _discover(_cfg("codex"), persist=True)
        assert list(found.roots) == [repo]
        assert _cached_units("codex")["a"] == [None, [str(repo)]]

    def test_unit_read_failure_is_a_completed_observation(self, home, monkeypatch):
        reads: list[str] = []
        units = {"immutable": None, "stamped": None}
        stamps = {"stamped": [7]}
        monkeypatch.setattr(rootprobes, "PROBERS", (_synthetic(units, stamps=stamps, reads=reads),))
        found = _discover(_cfg("codex"), persist=True)
        assert not found.exceeded and found.errors == ()
        cached = _cached_units("codex")
        assert "immutable" not in cached, "an immutable unit that failed is retried next push"
        assert cached["stamped"] == [[7], []]
        reads.clear()
        _discover(_cfg("codex"), persist=True)
        assert reads == ["immutable"]

    def test_prober_error_names_the_exception_type_only(self, home, monkeypatch):
        def boom(_home, _state, _deadline):
            raise PermissionError(13, "denied", "/secret/x")

        monkeypatch.setattr(
            rootprobes,
            "PROBERS",
            (rootprobes.RootProber("codex", "provider", boom, lambda *_: ([], True)),),
        )
        found = _discover(_cfg("codex"))
        assert found.errors == ("codex prober: PermissionError",)
        row = events.make_mm_push_event(
            device="dev", mm_version="1.0.0", discovery_errors=list(found.errors)
        )
        assert "/secret" not in json.dumps(row)

    def test_no_usage_reader_is_called_or_changed(self, home, monkeypatch):
        from mind_meld import host_usage
        from mind_meld.hosts import codex as codex_host

        calls: list[str] = []

        def forbidden(name):
            def reader(*_a, **_k):
                calls.append(name)
                raise AssertionError(f"discovery called usage reader {name}")

            return reader

        monkeypatch.setattr(host_usage, "read_muse_usage", forbidden("muse"))
        monkeypatch.setattr(host_usage, "read_agy_usage", forbidden("agy"))
        monkeypatch.setattr(host_usage, "read_cursor_usage", forbidden("cursor"))
        monkeypatch.setattr(codex_host, "read_codex_usage", forbidden("codex"))
        repo = _fake_repo(home / "repo")
        _codex_rollout(_codex_day(home) / "rollout-1.jsonl", str(repo))
        found = _discover(_cfg("codex", "muse", "agy", cursor=True))
        assert calls == [] and found.errors == ()
        assert found.probers_ran == ("agy", "muse", "cursor", "codex")
        assert list(found.roots) == [repo]

    def test_normalization_keeps_valid_probe_index_parts(self):
        cache = events._default_git_capture_cache()
        cache["probe_index"] = {
            "codex": {
                "units": {
                    "good": [[1], ["/r"]],
                    "immutable": [None, []],
                    "bad-shape": ["x"],
                    "bad-roots": [None, [1]],
                    "bad-stamp": ["s", []],
                },
                "state": {"archived_mtime_ns": 5},
            },
            "muse": {"units": "nope", "state": []},
            "weird": "nope",
            3: {"units": {}, "state": {}},
        }
        events._normalize_git_capture_cache(cache)
        assert cache["probe_index"] == {
            "codex": {
                "units": {"good": [[1], ["/r"]], "immutable": [None, []]},
                "state": {"archived_mtime_ns": 5},
            },
            "muse": {"units": {}, "state": {}},
        }
        broken = {"version": 1, "probe_index": "nope"}
        events._normalize_git_capture_cache(broken)
        assert broken["probe_index"] == {}
        assert events._default_git_capture_cache()["probe_index"] == {}

    def test_commit_repo_cursors_preserves_probe_index(self, home, monkeypatch):
        repo = _fake_repo(home / "repo")
        monkeypatch.setattr(rootprobes, "PROBERS", (_synthetic({"a": [str(repo)]}),))
        _discover(_cfg("codex"), persist=True)
        before = events.read_git_capture_cache()["probe_index"]
        assert before["codex"]["units"]["a"][1] == [str(repo)]
        now = datetime.now(timezone.utc)
        events.commit_repo_cursors(
            [{"type": "git-snapshot", "projects": [{"local_path": str(repo), "commits": []}]}],
            since=now - timedelta(days=1),
            cursor_ts=now,
        )
        after = events.read_git_capture_cache()
        assert after["probe_index"] == before
        assert str(repo) in after["repo_cursors"], "the commit wrote the cache"

    def test_manual_roots_still_validate_first_and_win_attribution(self, home, monkeypatch):
        repo = _fake_repo(home / "repo")
        monkeypatch.setattr(rootprobes, "PROBERS", (_synthetic({"a": [str(repo)]}),))
        config = {**_cfg("codex"), "retro": {"repo_roots": [str(repo)]}}
        found = _discover(config)
        assert found.attribution == (("manual", repo),)
        assert found.probers_ran == ("manual", "codex")


# ---------------------------------------------------------------------------
# T3: provider probers
# ---------------------------------------------------------------------------


class TestCodexProber:
    def test_short_first_line_is_parsed_whole(self, home, monkeypatch):
        repo = _fake_repo(home / "repo")
        _codex_rollout(_codex_day(home) / "rollout-a.jsonl", str(repo))
        spy = _spy_lines(monkeypatch)
        assert list(_discover(_cfg("codex")).roots) == [repo]
        assert spy == []

    def test_long_line_with_cwd_early_uses_the_head_scan(self, home, monkeypatch):
        repo = _fake_repo(home / "repo")
        rollout = _codex_rollout(_codex_day(home) / "rollout-a.jsonl", str(repo), after_cwd=20_000)
        assert len(rollout.read_text().splitlines()[0]) > 20_000
        spy = _spy_lines(monkeypatch)
        assert list(_discover(_cfg("codex")).roots) == [repo]
        assert spy == [], "the head scan found cwd without reading the whole line"

    def test_cwd_after_a_large_field_falls_back_to_the_first_line(self, home, monkeypatch):
        repo = _fake_repo(home / "repo")
        _codex_rollout(_codex_day(home) / "rollout-a.jsonl", str(repo), before_cwd=20_000)
        spy = _spy_lines(monkeypatch)
        assert list(_discover(_cfg("codex")).roots) == [repo]
        assert len(spy) == 1

    def test_a_first_record_that_is_not_session_meta_names_nothing(self, home):
        repo = _fake_repo(home / "repo")
        day = _codex_day(home)
        _codex_rollout(day / "rollout-a.jsonl", str(repo), first_type="turn_context")
        _codex_rollout(
            day / "rollout-b.jsonl", str(repo), first_type="turn_context", before_cwd=20_000
        )
        found = _discover(_cfg("codex"))
        assert found.roots == () and found.probers_ran == ("codex",)

    def test_archived_rollout_is_found(self, home):
        repo = _fake_repo(home / "repo")
        _codex_rollout(
            home / ".codex" / "archived_sessions" / "rollout-2026-10-04T08-12-00-xyz.jsonl",
            str(repo),
        )
        assert list(_discover(_cfg("codex"), persist=True).roots) == [repo]
        assert "archived/2026-10-04" in _cached_units("codex")

    def test_warm_second_call_reads_no_rollout(self, home, monkeypatch):
        repo = _fake_repo(home / "repo")
        _codex_rollout(_codex_day(home) / "rollout-a.jsonl", str(repo))
        _codex_rollout(
            home / ".codex" / "archived_sessions" / "rollout-2026-10-04T08-12-00-xyz.jsonl",
            str(repo),
        )
        _discover(_cfg("codex"), persist=True)
        read = _spy_rollouts(monkeypatch)
        listed: list[Path] = []
        original = rootprobes._archived_groups
        monkeypatch.setattr(
            rootprobes,
            "_archived_groups",
            lambda archived, *a: (listed.append(archived), original(archived, *a))[1],
        )
        assert list(_discover(_cfg("codex"), persist=True).roots) == [repo]
        assert read == []
        assert listed == [], "an unchanged archived directory is not listed again"

    def test_new_rollout_rereads_only_its_day(self, home, monkeypatch):
        repo = _fake_repo(home / "repo")
        other = _fake_repo(home / "other")
        old_day = _codex_day(home, "2026/11/01")
        new_day = _codex_day(home, "2026/11/03")
        _codex_rollout(old_day / "rollout-old.jsonl", str(repo))
        _codex_rollout(new_day / "rollout-first.jsonl", str(repo))
        _discover(_cfg("codex"), persist=True)
        read = _spy_rollouts(monkeypatch)
        _codex_rollout(new_day / "rollout-second.jsonl", str(other))
        _bump(new_day, 5_000_000)
        found = _discover(_cfg("codex"), persist=True)
        assert {p.name for p in found.roots} == {"repo", "other"}
        assert sorted(p.name for p in read) == ["rollout-first.jsonl", "rollout-second.jsonl"]

    def test_newest_day_is_indexed_first(self, home):
        units = rootprobes._codex_list(home, {}, None)
        assert units == []
        for day in ("2026/10/30", "2026/11/03", "2026/09/01"):
            _codex_rollout(_codex_day(home, day) / "rollout-a.jsonl", str(home))
        keys = [key for key, _stamp in rootprobes._codex_list(home, {}, None)]
        assert keys == ["live/2026/11/03", "live/2026/10/30", "live/2026/09/01"]

    def test_unreadable_listing_is_a_prober_error_not_an_empty_tree(self, home):
        sessions = home / ".codex" / "sessions"
        sessions.mkdir(parents=True)
        os.chmod(sessions, 0)
        try:
            if os.access(sessions, os.R_OK):
                pytest.skip("permissions are not enforced for this user")
            found = _discover(_cfg("codex"))
        finally:
            os.chmod(sessions, 0o700)
        assert found.errors == ("codex prober: PermissionError",)


def _spy_lines(monkeypatch) -> list[str]:
    """Record every whole-first-line fallback read."""
    calls: list[str] = []
    original = token_usage.iter_bounded_lines

    def spy(fh, path_str, *args, **kwargs):
        calls.append(path_str)
        return original(fh, path_str, *args, **kwargs)

    monkeypatch.setattr(token_usage, "iter_bounded_lines", spy)
    return calls


def _spy_rollouts(monkeypatch) -> list[Path]:
    read: list[Path] = []
    original = rootprobes._codex_rollout_cwd

    def spy(path):
        read.append(path)
        return original(path)

    monkeypatch.setattr(rootprobes, "_codex_rollout_cwd", spy)
    return read


class TestCursorProber:
    def _acp(self, home: Path, session: str, cwd: str) -> None:
        directory = home / ".cursor" / "acp-sessions" / session
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text(
            json.dumps({"cwd": cwd, "schemaVersion": 1, "title": "t"})
        )

    def _conductor(self, home: Path) -> Path:
        return home / "Library" / "Application Support" / "com.conductor.app" / "cursor-sdk-store"

    def test_acp_ndjson_and_index_db_all_name_roots(self, home):
        acp_repo = _fake_repo(home / "acp-repo")
        ndjson_repo = _fake_repo(home / "ndjson-repo")
        db_repo = _fake_repo(home / "db-repo")
        self._acp(home, "s1", str(acp_repo))
        store = self._conductor(home) / "store-1"
        store.mkdir(parents=True)
        (store / "agents.ndjson").write_text(
            json.dumps({"cwd": str(ndjson_repo)}) + "\nnot json\n" + json.dumps({"id": 1}) + "\n"
        )
        _make_db(store / "index.db", "agents", "workspace_ref", [str(db_repo), "relative", "x"])
        found = _discover(_cfg(cursor=True), persist=True)
        assert found.probers_ran == ("cursor",)
        assert {p.name for p in found.roots} == {"acp-repo", "ndjson-repo", "db-repo"}
        assert {source for source, _ in found.attribution} == {"cursor"}
        assert set(_cached_units("cursor")) == {
            "acp/s1",
            "conductor/store-1/agents.ndjson",
            "conductor/store-1/index.db",
        }

    def test_cursor_needs_its_usage_consent(self, home):
        self._acp(home, "s1", str(_fake_repo(home / "repo")))
        for config in (_cfg("cursor"), _cfg(), {"retro": {"cursor_host_usage": False}}):
            found = _discover(config)
            assert "cursor" not in found.probers_ran and found.roots == ()
        assert "cursor" in _discover(_cfg(cursor=True)).probers_ran

    def test_credential_neighbours_are_never_opened(self, home, monkeypatch):
        repo = _fake_repo(home / "repo")
        self._acp(home, "s1", str(repo))
        auth = home / ".cursor" / "projects" / "p" / "mcp-auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text('{"token": "never-read"}')
        store_db = home / ".cursor" / "chats" / "x" / "y" / "store.db"
        store_db.parent.mkdir(parents=True)
        store_db.write_bytes(b"not a database")
        (home / ".cursor" / "cli-config.json").write_text("{}")
        (home / ".paseo").mkdir()
        (home / ".paseo" / "config.json").write_text('{"secret": 1}')
        oauth = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        oauth.parent.mkdir(parents=True)
        oauth.write_text("secret")
        _make_db(
            home / ".gemini" / "antigravity-cli" / "conversation_summaries.db",
            "conversation_summaries",
            "workspace_uris",
            [json.dumps([repo.as_uri()])],
        )

        touched: list[str] = []

        def recording(real):
            def spy(*args, **kwargs):
                target = args[0] if args else next(iter(kwargs.values()), "")
                touched.append(str(target))
                return real(*args, **kwargs)

            return spy

        monkeypatch.setattr(builtins, "open", recording(builtins.open))
        monkeypatch.setattr(os, "open", recording(os.open))
        monkeypatch.setattr(sqlite3, "connect", recording(sqlite3.connect))
        monkeypatch.setattr(os, "scandir", recording(os.scandir))
        found = _discover(_cfg("agy", cursor=True))
        assert list(found.roots) == [repo]
        ours = [t for t in touched if str(home) in t]
        assert ours, "the spy saw the probers' own reads"
        for forbidden in ("mcp-auth", "store.db", "oauth-token", "config.json", "cli-config"):
            assert not [t for t in ours if forbidden in t], forbidden
        for directory in (".cursor/projects", ".cursor/chats"):
            assert not [t for t in ours if directory in t], directory

    def test_index_db_with_a_hot_journal_is_skipped_until_it_settles(self, home):
        repo = _fake_repo(home / "repo")
        store = self._conductor(home) / "store-1"
        store.mkdir(parents=True)
        _make_db(store / "index.db", "agents", "workspace_ref", [str(repo)])
        (store / "index.db-journal").write_bytes(b"")
        assert _discover(_cfg(cursor=True), persist=True).roots == ()
        (store / "index.db-journal").unlink()
        assert list(_discover(_cfg(cursor=True), persist=True).roots) == [repo]

    def test_acp_session_without_meta_is_retried(self, home):
        # Value: protects=late Cursor cwd is discovered after missing or incomplete metadata;
        # fails_when=an empty ACP observation becomes a permanent negative cache;
        # why_new=the existing assertion checked cache absence but never a later successful read;
        # seam=none; no_claim=live Cursor format acceptance (synthetic metadata).
        (home / ".cursor" / "acp-sessions" / "fresh").mkdir(parents=True)
        found = _discover(_cfg(cursor=True), persist=True)
        assert found.roots == () and not found.exceeded
        assert "acp/fresh" not in _cached_units("cursor")
        metadata = home / ".cursor" / "acp-sessions" / "fresh" / "meta.json"
        metadata.write_text("{}")
        assert _discover(_cfg(cursor=True), persist=True).roots == ()
        repo = _fake_repo(home / "repo")
        metadata.write_text(json.dumps({"cwd": str(repo)}))
        assert _discover(_cfg(cursor=True), persist=True).roots == (repo,)


class TestMuseProber:
    def _session(self, home: Path, key: str, records: list[dict]) -> None:
        directory = home / ".local" / "share" / "muse" / "sessions" / key
        directory.mkdir(parents=True)
        (directory / "session.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n"
        )

    def test_both_workspace_root_shapes(self, home):
        direct = _fake_repo(home / "direct")
        nested = _fake_repo(home / "nested")
        self._session(
            home,
            "2026/11/03/s-direct",
            [
                {"payload": {"record": {"kind": "x"}}},
                {"payload": {"record": {"workspace_root": str(direct)}}},
            ],
        )
        self._session(
            home,
            "2026/11/02/s-nested",
            [
                {"payload": {"record": {"kind": "x"}}},
                {
                    "payload": {
                        "record": {"command": {"session_metadata": {"workspace_root": str(nested)}}}
                    }
                },
            ],
        )
        found = _discover(_cfg("muse"), persist=True)
        assert {p.name for p in found.roots} == {"direct", "nested"}
        assert [key for key, _ in rootprobes._muse_list(home, {}, None)] == [
            "2026/11/03/s-direct",
            "2026/11/02/s-nested",
        ]

    def test_only_the_first_sixteen_records_are_read(self, home):
        repo = _fake_repo(home / "late")
        records = [{"payload": {"record": {"kind": "x"}}}] * 16
        self._session(
            home,
            "2026/11/03/s",
            [*records, {"payload": {"record": {"workspace_root": str(repo)}}}],
        )
        assert _discover(_cfg("muse")).roots == ()

    def test_muse_needs_its_source(self, home):
        repo = _fake_repo(home / "repo")
        self._session(
            home, "2026/11/03/s", [{"payload": {"record": {"workspace_root": str(repo)}}}]
        )
        assert "muse" not in _discover(_cfg("codex")).probers_ran
        assert "muse" not in _discover(_cfg("muse", disabled=("muse",))).probers_ran
        assert list(_discover(_cfg("muse")).roots) == [repo]


class TestAntigravityProber:
    def _summaries(self, home: Path) -> Path:
        return home / ".gemini" / "antigravity-cli" / "conversation_summaries.db"

    def test_percent_encoded_uris_with_spaces_and_brackets(self, home):
        repo = _fake_repo(home / "my repo <x>")
        uri = "file://" + quote(str(repo))
        assert "%20" in uri and "%3C" in uri and "%3E" in uri
        _make_db(
            self._summaries(home),
            "conversation_summaries",
            "workspace_uris",
            [
                json.dumps([uri, "https://example.com/x", "file://other-host/x"]),
                "not json",
                json.dumps({"not": "a list"}),
            ],
        )
        found = _discover(_cfg("agy"), persist=True)
        assert list(found.roots) == [repo]
        assert found.attribution == (("agy", repo),)

    def test_live_wal_rows_are_visible(self, home):
        repo = _fake_repo(home / "repo")
        database = self._summaries(home)
        database.parent.mkdir(parents=True)
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("create table conversation_summaries (workspace_uris text)")
            connection.execute(
                "insert into conversation_summaries values (?)", (json.dumps([repo.as_uri()]),)
            )
            connection.commit()
            assert database.with_name(database.name + "-wal").exists()
            assert list(_discover(_cfg("agy")).roots) == [repo]
        finally:
            connection.close()

    def test_unchanged_database_is_not_read_again(self, home, monkeypatch):
        repo = _fake_repo(home / "repo")
        _make_db(
            self._summaries(home),
            "conversation_summaries",
            "workspace_uris",
            [json.dumps([repo.as_uri()])],
        )
        _discover(_cfg("agy"), persist=True)
        reads: list[str] = []
        original = rootprobes._sqlite_first_column
        monkeypatch.setattr(
            rootprobes,
            "_sqlite_first_column",
            lambda *a, **k: (reads.append("read"), original(*a, **k))[1],
        )
        assert list(_discover(_cfg("agy"), persist=True).roots) == [repo]
        assert reads == []


class TestProviderGates:
    def test_providers_run_only_when_enabled(self, home):
        repo = _fake_repo(home / "repo")
        _codex_rollout(_codex_day(home) / "rollout-a.jsonl", str(repo))
        assert "codex" not in _discover(_cfg("muse")).probers_ran
        assert "codex" not in _discover(_cfg("codex", disabled=("codex",))).probers_ran
        assert "codex" not in _discover({"sync": {"sources": []}}).probers_ran
        assert _discover(_cfg("codex")).probers_ran == ("codex",)

    def test_implicit_config_runs_codex_when_the_directory_exists(self, home):
        repo = _fake_repo(home / "repo")
        assert "codex" not in _discover({}).probers_ran
        _codex_rollout(_codex_day(home) / "rollout-a.jsonl", str(repo))
        found = _discover({})
        assert found.probers_ran == ("claude", "codex")
        assert list(found.roots) == [repo]
        assert "codex" not in _discover({"sync": {"disabled_sources": ["codex"]}}).probers_ran
        # muse and agy are opt-in sources: an implicit config never names them.
        assert {"muse", "agy"}.isdisjoint(found.probers_ran)


# ---------------------------------------------------------------------------
# T4: tool probers and worktree deduplication
# ---------------------------------------------------------------------------


class TestToolProbers:
    def _paseo(self, home: Path, *root_paths: str, extra: list | None = None) -> None:
        path = home / ".paseo" / "projects" / "projects.json"
        path.parent.mkdir(parents=True)
        items = [{"projectId": f"p{i}", "rootPath": root} for i, root in enumerate(root_paths)]
        path.write_text(json.dumps(items + (extra or [])))

    def test_paseo_root_with_spaces_is_attributed_to_paseo(self, home):
        repo = _fake_repo(home / "My Projects" / "repo with spaces")
        self._paseo(
            home,
            str(repo),
            extra=[
                {"projectId": "gone", "rootPath": str(home / "missing"), "archivedAt": "2026-01-01"}
            ],
        )
        found = _discover(_cfg(), persist=True)
        assert found.probers_ran == ("paseo",)
        assert found.attribution == (("paseo", repo),)

    def test_archived_paseo_projects_still_count(self, home):
        repo = _fake_repo(home / "old")
        path = home / ".paseo" / "projects" / "projects.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([{"rootPath": str(repo), "archivedAt": "2026-01-01"}]))
        assert list(_discover(_cfg()).roots) == [repo]

    def test_mmm_item_repo_becomes_a_root(self, home):
        repo = _fake_repo(home / "repo")
        items = home / ".mmm" / "items"
        items.mkdir(parents=True)
        (items / "a.json").write_text(json.dumps({"repo": str(repo), "worktree": str(home / "x")}))
        (items / "b.json").write_text("not json")
        (items / "a.plan.md").write_text("# not read")
        (home / ".mmm" / "manager.toml").write_text("secret = 1")
        found = _discover(_cfg(), persist=True)
        assert found.probers_ran == ("mmm",)
        assert found.attribution == (("mmm", repo),)
        assert set(_cached_units("mmm")) == {"items/a.json"}, "an unreadable item is retried"

    def test_tools_that_are_not_installed_do_not_run(self, home):
        (home / ".paseo").mkdir()
        (home / ".mmm").mkdir()
        found = _discover(_cfg("codex", "muse", "agy", cursor=True))
        assert "paseo" not in found.probers_ran and "mmm" not in found.probers_ran

    def test_gstack_project_state_runs_no_prober(self, home):
        project = home / ".gstack" / "projects" / "owner-repo"
        project.mkdir(parents=True)
        (project / "repo-mode.json").write_text(
            json.dumps({"mode": "solo", "top_pct": 100, "authors": 1, "total": 3, "computed": "x"})
        )
        found = _discover({})
        assert "gstack" not in found.probers_ran
        assert found.probers_ran == ("claude",)

    def test_installed_checks_the_named_record_only(self, home):
        paseo, mmm = rootprobes.PROBERS[0], rootprobes.PROBERS[1]
        assert (paseo.name, mmm.name) == ("paseo", "mmm")
        assert not rootprobes.installed(paseo, home) and not rootprobes.installed(mmm, home)
        (home / ".paseo").mkdir()
        (home / ".mmm").mkdir()
        assert not rootprobes.installed(paseo, home) and not rootprobes.installed(mmm, home)
        self._paseo(home, str(home))
        (home / ".mmm" / "items").mkdir()
        assert rootprobes.installed(paseo, home) and rootprobes.installed(mmm, home)

    def test_paseo_main_repo_wins_over_a_worktree_named_by_a_provider(
        self, home, tmp_path, monkeypatch
    ):
        _git_env_repos(tmp_path, monkeypatch)
        main = home / "main"
        _init_git_repo(main)
        worktree = home / ".paseo" / "worktrees" / "abc" / "repo"
        worktree.parent.mkdir(parents=True)
        _make_git_worktree(main, worktree)
        self._paseo(home, str(main))
        _codex_rollout(_codex_day(home) / "rollout-a.jsonl", str(worktree))
        found = _discover(_cfg("codex"), persist=True)
        assert found.roots == (main.resolve(),)
        assert found.attribution == (("paseo", main.resolve()),)
        # Codex did name the worktree; it is the same repository, so it folds in.
        assert _cached_units("codex")["live/2026/11/03"][1] == [str(worktree.resolve())]

    def test_worktrees_of_one_repository_collapse_to_one_root(self, home, tmp_path, monkeypatch):
        _git_env_repos(tmp_path, monkeypatch)
        main = tmp_path / "elsewhere" / "main"
        _init_git_repo(main)
        first = home / ".paseo" / "worktrees" / "abc" / "one"
        second = home / ".paseo" / "worktrees" / "def" / "two"
        first.parent.mkdir(parents=True)
        _make_git_worktree(main, first, branch="wt-one")
        second.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", "-b", "wt-two", str(second)], check=True
        )
        for name, path in (("s1", first), ("s2", second)):
            directory = home / ".cursor" / "acp-sessions" / name
            directory.mkdir(parents=True)
            (directory / "meta.json").write_text(json.dumps({"cwd": str(path)}))
        found = _discover(_cfg(cursor=True), persist=True)
        assert len(found.roots) == 1
        assert found.roots[0] in {first.resolve(), second.resolve()}
        assert found.attribution[0][0] == "cursor"


# ---------------------------------------------------------------------------
# T6: a newly found repository's earlier commits need `mm recapture`
# ---------------------------------------------------------------------------


def _commit_at(repo: Path, message: str, when: datetime) -> None:
    stamp = when.isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", message],
        check=True,
        env=env,
    )


def _captured_messages(rows: list[dict]) -> set[str]:
    return {
        commit["subject"]
        for row in rows
        for project in row.get("projects", [])
        for commit in project.get("commits", [])
    }


def test_new_root_walks_from_the_push_cursor_and_recapture_reaches_history(
    home, tmp_path, monkeypatch
):
    """Criterion 8: a repository found for the first time is captured from the
    push cursor forward; commits older than that need ``mm recapture <window>``."""
    _git_env_repos(tmp_path, monkeypatch)
    repo = home / "dev" / "found-by-paseo"
    _init_git_repo(repo)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _commit_at(repo, "ten days ago", now - timedelta(days=10))
    _commit_at(repo, "just now", now - timedelta(minutes=5))
    path = home / ".paseo" / "projects" / "projects.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"projectId": "p", "rootPath": str(repo)}]))

    rows, discovery, _aborts, errors = events_tail._capture_git_rows(
        {},
        "dev",
        since=now - timedelta(days=2),
        walk_budget_ms=10_000,
        root_discovery_budget_ms=10_000,
        persist=True,
        use_repo_cursors=True,
    )
    assert errors == 0 and discovery.attribution == (("paseo", repo.resolve()),)
    assert _captured_messages(rows) == {"just now"}

    rows, _discovery, _aborts, errors = events_tail._capture_git_rows(
        {},
        "dev",
        since=now - timedelta(days=30),
        walk_budget_ms=10_000,
        root_discovery_budget_ms=10_000,
        origin=events.GIT_SNAPSHOT_ORIGIN_RECAPTURE,
        persist=True,
        use_repo_cursors=True,
    )
    assert errors == 0
    assert _captured_messages(rows) == {"ten days ago", "just now"}


def test_muse_metadata_arriving_after_initial_records_is_retried(home):
    repo = _fake_repo(home / "repo")
    path = home / ".local/share/muse/sessions/2026/11/03/fresh/session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"payload":{"record":{"kind":"started"}}}\n')
    assert _discover(_cfg("muse"), persist=True).roots == ()
    with path.open("a") as stream:
        stream.write(json.dumps({"payload": {"record": {"workspace_root": str(repo)}}}) + "\n")
    assert _discover(_cfg("muse"), persist=True).roots == (repo,)


def test_codex_day_keeps_progress_between_budgeted_pushes(home, monkeypatch):
    repo = _fake_repo(home / "repo")
    for i in range(6):
        _codex_rollout(_codex_day(home) / f"rollout-{i}.jsonl", str(repo))
    clock = [0.0]
    original = rootprobes._codex_rollout_cwd
    reads = []

    def read(path):
        reads.append(path.name)
        clock[0] += 1.0
        return original(path)

    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rootprobes, "_codex_rollout_cwd", read)
    for _ in range(4):
        result = _discover(_cfg("codex"), persist=True, seconds=2.5)
        if not result.exceeded:
            break
    assert not result.exceeded and result.roots == (repo,)
    # Only the record whose read crossed the deadline is replayed: its cwd
    # was not classified, so its filename cannot be acknowledged yet.
    assert reads == [f"rollout-{i}.jsonl" for i in (0, 1, 2, 2, 3, 4, 4, 5)]
    assert not events.read_git_capture_cache()["probe_index"]["codex"]["state"].get("pending_units")


def test_codex_empty_rollout_is_retried_after_first_record_arrives(home):
    repo = _fake_repo(home / "repo")
    path = _codex_day(home) / "rollout-a.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    other = _fake_repo(home / "other")
    _codex_rollout(path.with_name("rollout-b.jsonl"), str(other))
    result = _discover(_cfg("codex"), persist=True)
    assert not result.exceeded and result.roots == (other,)
    assert not _discover(_cfg("codex"), persist=True).exceeded
    _codex_rollout(path, str(repo))
    result = _discover(_cfg("codex"), persist=True)
    assert not result.exceeded and set(result.roots) == {repo, other}


def test_previously_known_root_returns_after_temporary_absence(home):
    repo = _fake_repo(home / "repo")
    path = home / ".paseo/projects/projects.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"rootPath": str(repo)}]))
    assert _discover(_cfg(), persist=True).roots == (repo,)
    (repo / ".git").rename(repo / ".git-away")
    assert _discover(_cfg(), persist=True).roots == ()
    assert _cached_units("paseo")["projects.json"][1] == []
    (repo / ".git-away").rename(repo / ".git")
    assert _discover(_cfg(), persist=True).roots == (repo,)
    path.write_text("[]")
    assert _discover(_cfg(), persist=True).roots == ()


def test_muse_read_cap_bounds_input_before_oversize_drain(home, monkeypatch, capsys):
    import io

    class Counted(io.BytesIO):
        count = 0

        def readline(self, size=-1):
            raw = super().readline(size)
            self.count += len(raw)
            return raw

        def read(self, size=-1):
            raw = super().read(size)
            self.count += len(raw)
            return raw

    stream = Counted(b"x" * 1024)
    monkeypatch.setattr(rootprobes, "_open_regular", lambda path: stream)
    monkeypatch.setattr(rootprobes, "_MUSE_CAP", 32)
    monkeypatch.setattr(token_usage, "MAX_JSONL_LINE_BYTES", 16)
    rootprobes._muse_read(home, "2026/11/03/s", None)
    assert stream.count <= 32
    assert capsys.readouterr().err == ""


def test_cursor_stream_keeps_progress_between_budgeted_pushes(home, monkeypatch):
    repos = [_fake_repo(home / f"repo-{i}") for i in range(6)]
    path = home.joinpath(*rootprobes._CONDUCTOR_STORE) / "store" / "agents.ndjson"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps({"cwd": str(repo)}) + "\n" for repo in repos))
    clock = [0.0]
    original = rootprobes._loads
    reads = []

    def load(raw):
        result = original(raw)
        if isinstance(result, dict) and "cwd" in result:
            reads.append(result["cwd"])
            clock[0] += 1.0
        return result

    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rootprobes, "_loads", load)
    for _ in range(4):
        result = _discover(_cfg(cursor=True), persist=True, seconds=2.5)
        if not result.exceeded:
            break
    assert not result.exceeded and set(result.roots) == set(repos)
    assert reads == [str(repos[i]) for i in (0, 1, 2, 2, 3, 4, 4, 5)]


def test_pending_unit_restarts_on_change_and_is_pruned_when_vanished(home, monkeypatch):
    first = _fake_repo(home / "first")
    second = _fake_repo(home / "second")
    day = _codex_day(home)
    _codex_rollout(day / "rollout-a.jsonl", str(first))
    _codex_rollout(day / "rollout-b.jsonl", str(second))
    clock = [0.0]
    original = rootprobes._codex_rollout_cwd

    def read(path):
        clock[0] += 1.0
        return original(path)

    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rootprobes, "_codex_rollout_cwd", read)
    assert _discover(_cfg("codex"), persist=True, seconds=0.5).exceeded
    _codex_rollout(day / "rollout-a.jsonl", str(second))
    _bump(day, 1_000_000)
    assert _discover(_cfg("codex"), persist=True).roots == (second,)
    shutil.rmtree(day)
    assert _discover(_cfg("codex"), persist=True).roots == ()
    state = events.read_git_capture_cache()["probe_index"]["codex"]["state"]
    assert state["pending_units"] == {} and state["retry_roots"] == {}


def test_retry_roots_exclude_never_repositories_and_follow_record_lifetime(home):
    repo = _fake_repo(home / "repo")
    ordinary = home / "ordinary"
    ordinary.mkdir()
    path = home / ".paseo/projects/projects.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"rootPath": str(p)} for p in (repo, ordinary)]))
    _discover(_cfg(), persist=True)
    (repo / ".git").rename(repo / ".git-away")
    _discover(_cfg(), persist=True)
    record = events.read_git_capture_cache()["probe_index"]["paseo"]
    assert record["state"]["retry_roots"]["projects.json"][1] == [str(repo)]
    assert str(ordinary) not in json.dumps(record)
    path.write_text("[]")
    _discover(_cfg(), persist=True)
    assert events.read_git_capture_cache()["probe_index"]["paseo"]["state"]["retry_roots"] == {}
    path.unlink()
    _discover(_cfg(), persist=True)
    assert "paseo" not in events.read_git_capture_cache()["probe_index"]


def test_agy_malformed_sqlite_value_does_not_hide_valid_rows(home):
    repo = _fake_repo(home / "repo")
    path = home.joinpath(*rootprobes._AGY_SUMMARIES)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute("create table conversation_summaries (workspace_uris)")
        conn.executemany(
            "insert into conversation_summaries values (?)",
            [(42,), (json.dumps([repo.as_uri()]),)],
        )
    assert _discover(_cfg("agy")).roots == (repo,)


def test_cursor_cap_preserves_roots_from_complete_prefix(home, monkeypatch, capsys):
    repo = _fake_repo(home / "repo")
    path = home.joinpath(*rootprobes._CONDUCTOR_STORE) / "store" / "agents.ndjson"
    path.parent.mkdir(parents=True)
    first = json.dumps({"cwd": str(repo)}) + "\n"
    path.write_text(first + "x" * 128)
    monkeypatch.setattr(rootprobes, "_CURSOR_NDJSON_CAP", len(first.encode()) + 16)
    result = _discover(_cfg(cursor=True), persist=True)
    assert not result.exceeded and result.roots == (repo,)
    assert capsys.readouterr().err == ""


def test_codex_pending_retries_share_progress_across_pushes(home, monkeypatch):
    repo = _fake_repo(home / "repo")
    day = _codex_day(home)
    day.mkdir(parents=True)
    for name in ("a", "b", "c"):
        (day / f"rollout-{name}.jsonl").write_bytes(b"")
    assert not _discover(_cfg("codex"), persist=True).exceeded
    reader = events.read_git_capture_cache()["probe_index"]["codex"]["state"]["pending_units"][
        "live/2026/11/03"
    ]["reader"]
    assert reader["scan_complete"] and "listing_steps" not in reader
    _codex_rollout(day / "rollout-c.jsonl", str(repo))
    clock = [0.0]
    original = rootprobes._codex_rollout_cwd
    reads = []

    def read(path):
        reads.append(path.name)
        clock[0] += 1.0
        return original(path)

    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rootprobes, "_codex_rollout_cwd", read)
    for _ in range(3):
        result = _discover(_cfg("codex"), persist=True, seconds=1.5)
        if result.roots:
            break
    assert result.roots == (repo,)
    assert reads == ["rollout-a.jsonl", "rollout-b.jsonl", "rollout-c.jsonl"]


@pytest.mark.parametrize("provider", ["muse", "cursor", "codex"])
def test_greptile_listing_progress_survives_push_boundaries(home, monkeypatch, provider):
    repo = _fake_repo(home / "repo")
    for i in range(6):
        if provider == "muse":
            path = home / f".local/share/muse/sessions/2026/11/{i + 1:02d}/s/session.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"payload": {"record": {"workspace_root": str(repo)}}}))
        elif provider == "cursor":
            path = home / f".cursor/acp-sessions/s{i}/meta.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"cwd": str(repo)}))
        else:
            _codex_rollout(_codex_day(home, f"2026/11/{i + 1:02d}") / "rollout-a.jsonl", str(repo))
    clock = [0.0]
    scandir = os.scandir
    listdir = os.listdir

    class SlowListing:
        def __init__(self, path):
            self.inner = scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.inner.close()

        def __iter__(self):
            for entry in self.inner:
                clock[0] += 1
                yield entry

    def slow_listdir(path):
        clock[0] += 1
        return listdir(path)

    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rootprobes.os, "scandir", SlowListing)
    monkeypatch.setattr(rootprobes.os, "listdir", slow_listdir)
    prober = next(p for p in rootprobes.PROBERS if p.name == provider)
    state = {}
    for _ in range(30):
        listing = prober.list_units(home, state, clock[0] + 2.5)
        state = json.loads(json.dumps(state))  # a fresh process only gets persisted state
        if listing is not None:
            break
    assert listing is not None and len(listing) == 6


def test_greptile_unfinished_units_cannot_starve_unread_units(home, monkeypatch):
    repo = _fake_repo(home / "repo")
    clock = [0.0]
    reads = []
    prober = _synthetic({"a": None, "b": None, "c": [str(repo)]}, reads=reads, clock=clock)
    cache = events._default_git_capture_cache()
    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    for _ in range(3):
        roots = events._probe_host_records(prober, cache=cache, deadline_monotonic=clock[0] + 1.5)
        cache = json.loads(json.dumps(cache))
        if roots:
            break
    assert roots == [repo] and reads[:3] == ["a", "b", "c"]


def test_greptile_classification_is_bounded_and_resumes(home, monkeypatch):
    repos = [_fake_repo(home / f"repo-{i}") for i in range(6)]
    ordinary = home / "ordinary"
    ordinary.mkdir()
    prober = _synthetic({"one": [str(ordinary), *map(str, repos)]})
    clock = [0.0]
    classified = []
    original = events._classify_git_root

    def classify(path):
        classified.append(path)
        clock[0] += 1
        return original(path)

    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(events, "_classify_git_root", classify)
    cache = events._default_git_capture_cache()
    for _ in range(4):
        before = len(classified)
        roots = events._probe_host_records(prober, cache=cache, deadline_monotonic=clock[0] + 2.5)
        assert len(classified) - before <= 3
        assert str(ordinary) not in json.dumps(cache)
        cache = json.loads(json.dumps(cache))
        if cache["_discovery_complete"]:
            break
    assert set(roots) == set(repos) and cache["_discovery_complete"]
    assert len(classified) == 7


def test_greptile_reader_acknowledgment_skips_costly_classified_prefix(home, monkeypatch):
    repos = [_fake_repo(home / f"repo-{i}") for i in range(6)]
    path = home.joinpath(*rootprobes._CONDUCTOR_STORE) / "store/agents.ndjson"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps({"cwd": str(repo)}) + "\n" for repo in repos))
    clock = [0.0]
    reads = []
    original_load = rootprobes._loads
    original_classify = events._classify_git_root

    def load(raw):
        value = original_load(raw)
        if isinstance(value, dict) and "cwd" in value:
            reads.append(value["cwd"])
            clock[0] += 0.6
        return value

    def classify(path):
        clock[0] += 0.75
        return original_classify(path)

    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rootprobes, "_loads", load)
    monkeypatch.setattr(events, "_classify_git_root", classify)
    for _ in range(10):
        result = _discover(_cfg(cursor=True), persist=True, seconds=2.5)
        if not result.exceeded:
            break
    assert set(result.roots) == set(repos) and not result.exceeded
    assert reads == [str(repo) for repo in repos]


def test_greptile_later_provider_gets_a_turn_when_earlier_stays_incomplete(home, monkeypatch):
    repo = _fake_repo(home / "repo")
    clock = [0.0]

    def never_listed(*_args):
        clock[0] += 3
        return None

    early = rootprobes.RootProber("muse", "provider", never_listed, lambda *_: ([], True))
    later = _synthetic({"one": [str(repo)]})
    monkeypatch.setattr(rootprobes, "PROBERS", (early, later))
    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    first = _discover(_cfg("muse", "codex"), persist=True, seconds=2.5)
    second = _discover(_cfg("muse", "codex"), persist=True, seconds=2.5)
    assert first.exceeded and first.roots == ()
    assert second.exceeded and second.roots == (repo,)
    assert second.attribution == (("codex", repo),)


def test_greptile_archive_snapshot_keeps_its_original_stamp(home, monkeypatch):
    first = _fake_repo(home / "first")
    later = _fake_repo(home / "later")
    archive = home / ".codex/archived_sessions"
    for name in ("a", "b"):
        _codex_rollout(archive / f"rollout-2026-11-03-{name}.jsonl", str(first))
    clock = [0.0]
    original = rootprobes._lstat

    def stat(path):
        result = original(path)
        if path.parent == archive:
            clock[0] += 1
        return result

    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rootprobes, "_lstat", stat)
    prober = next(p for p in rootprobes.PROBERS if p.name == "codex")
    cache = events._default_git_capture_cache()
    events._probe_host_records(prober, cache=cache, deadline_monotonic=0.5)
    assert not cache["_discovery_complete"]
    _codex_rollout(archive / "rollout-2026-11-03-c.jsonl", str(later))
    _bump(archive, 1_000_000)
    for _ in range(4):
        roots = events._probe_host_records(prober, cache=cache, deadline_monotonic=clock[0] + 20)
        cache = json.loads(json.dumps(cache))
    assert later in roots


def test_greptile_listing_prefix_validation_has_linear_membership_work(home):
    class Counted(str):
        comparisons = 0
        __hash__ = str.__hash__

        def __eq__(self, other):
            type(self).comparisons += 1
            return str.__eq__(self, other)

    names = [Counted(f"entry-{i}") for i in range(400)]
    state = {
        "listing_steps": {
            str(home): {
                "names": names,
                "offset": len(names),
                "entries": [[str(name), 0, 0, 0] for name in names],
            }
        }
    }
    assert len(rootprobes._listing_entries(home, state, None)) == len(names)
    assert Counted.comparisons <= 4 * len(names)


@pytest.mark.parametrize("incomplete", [False, True])
def test_greptile_retry_round_refreshes_inventory_even_when_records_stay_bad(
    home, monkeypatch, incomplete
):
    repo = _fake_repo(home / "repo")
    records = {"a": None, "b": None, "c": None}
    clock = [0.0]
    original = _synthetic(records, clock=clock)

    def read(home, key, deadline):
        values, done = original.read_unit(home, key, deadline)
        return values, done and not (incomplete and values is None)

    prober = rootprobes.RootProber("codex", "provider", original.list_units, read)
    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    cache = events._default_git_capture_cache()
    for _ in range(2):
        events._probe_host_records(prober, cache=cache, deadline_monotonic=clock[0] + 1.5)
    records["new"] = [str(repo)]
    for _ in range(4):
        roots = events._probe_host_records(prober, cache=cache, deadline_monotonic=clock[0] + 1.5)
        cache = json.loads(json.dumps(cache))
        if repo in roots:
            break
    assert repo in roots


def test_greptile_resolution_deadline_keeps_cached_roots(home, monkeypatch):
    repos = [_fake_repo(home / name) for name in ("a", "b")]
    monkeypatch.setattr(rootprobes, "PROBERS", (_synthetic({"one": list(map(str, repos))}),))
    _discover(_cfg("codex"), persist=True)
    clock = [0.0]
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path in repos:
            clock[0] += 2
        return result

    monkeypatch.setattr(events.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(Path, "resolve", resolve)
    result = _discover(_cfg("codex"), persist=True, seconds=1)
    assert result.exceeded and set(result.roots) == set(repos)


def test_greptile_unresolvable_root_does_not_disable_other_root_validation(home, monkeypatch):
    broken, deleted = [_fake_repo(home / name) for name in ("a", "b")]
    monkeypatch.setattr(rootprobes, "PROBERS", (_synthetic({"one": [str(broken), str(deleted)]}),))
    _discover(_cfg("codex"), persist=True)
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == broken:
            raise RuntimeError("synthetic symlink loop")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    _discover(_cfg("codex"), persist=True)
    shutil.rmtree(deleted / ".git")
    assert deleted not in _discover(_cfg("codex"), persist=True).roots


def test_greptile_rotation_preserves_canonical_source_and_disabled_cleanup(home, monkeypatch):
    repo = _fake_repo(home / "repo")
    early = _synthetic({"one": [str(repo)]})
    late = rootprobes.RootProber("muse", "provider", early.list_units, early.read_unit)
    monkeypatch.setattr(rootprobes, "PROBERS", (early, late))
    cache = events._default_git_capture_cache()
    cache["prober_resume_after"] = "codex"
    read_cache = events.read_git_capture_cache
    monkeypatch.setattr(events, "read_git_capture_cache", lambda: cache)
    result = _discover(_cfg("codex", "muse"), persist=True)
    assert result.probers_ran == ("muse", "codex")
    assert result.attribution == (("codex", repo),)
    cache = read_cache()
    assert cache["prober_resume_after"] is None
    _discover(_cfg("codex", "muse", disabled=("codex", "muse")), persist=True)
    cache = read_cache()
    assert cache["probe_index"] == {} and cache["prober_resume_after"] is None


def test_cursor_record_symlink_is_not_followed_and_replacement_is_read(home):
    # Value: protects=record-only discovery refuses a symlink at the named metadata file;
    # fails_when=the reader follows a metadata symlink into an unrelated local file;
    # why_new=credential-neighbour tests use ordinary neighbours rather than record symlinks;
    # seam=none; no_claim=all filesystem races or live Cursor stores.
    repo = _fake_repo(home / "repo")
    unrelated = home / "unrelated.json"
    unrelated.write_text(json.dumps({"cwd": str(repo)}))
    metadata = home / ".cursor" / "acp-sessions" / "s" / "meta.json"
    metadata.parent.mkdir(parents=True)
    metadata.symlink_to(unrelated)
    result = _discover(_cfg(cursor=True), persist=True)
    assert result.roots == () and not result.exceeded
    metadata.unlink()
    metadata.write_text(json.dumps({"cwd": str(repo)}))
    assert _discover(_cfg(cursor=True), persist=True).roots == (repo,)


def test_sqlite_unreadable_record_recovers_after_replacement_and_deadline_is_incomplete(home):
    # Value: protects=SQLite read failure stays nonfatal and an expired read is incomplete;
    # fails_when=corrupt databases crash discovery or a deadline is cached as a complete read;
    # why_new=existing SQLite tests cover valid rows/WAL but not corrupt input or expired reads;
    # seam=none; no_claim=live database contention or provider acceptance (synthetic SQLite).
    repo = _fake_repo(home / "repo")
    database = home.joinpath(*rootprobes._AGY_SUMMARIES)
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a sqlite database")
    result = _discover(_cfg("agy"), persist=True)
    assert result.roots == () and result.errors == () and not result.exceeded
    database.unlink()
    _make_db(database, "conversation_summaries", "workspace_uris", [json.dumps([repo.as_uri()])])
    assert _discover(_cfg("agy"), persist=True).roots == (repo,)
    assert rootprobes._agy_read(home, "conversation_summaries.db", -1.0) == (None, False)
    assert rootprobes._cursor_read(home, "conductor/store/index.db", -1.0) == (None, False)


def test_malformed_resume_state_is_replaced_without_losing_discovered_roots(home):
    # Value: protects=discovery recovers from malformed machine-local resume and retry state;
    # fails_when=bad persisted state crashes a scan or suppresses a valid current root;
    # why_new=normalization tests cover unit records but not the new per-unit resume maps;
    # seam=none; no_claim=arbitrary cache corruption outside the changed probe-index schema.
    repo = _fake_repo(home / "repo")
    _codex_rollout(_codex_day(home) / "rollout-a.jsonl", str(repo))
    _discover(_cfg("codex"), persist=True)
    cache = events.read_git_capture_cache()
    state = cache["probe_index"]["codex"]["state"]
    state["pending_units"] = {
        "live/2026/11/03": {"stamp": [], "reader": [], "roots": [7]},
        "vanished": {"reader": {}, "roots": [str(home / "absent")]},
    }
    state["retry_roots"] = {"live/2026/11/03": ["invalid", [str(repo)]]}
    mm_config.GIT_CAPTURE_CACHE_PATH.write_text(json.dumps(cache))
    result = _discover(_cfg("codex"), persist=True)
    assert result.roots == (repo,) and result.errors == () and not result.exceeded
    state = events.read_git_capture_cache()["probe_index"]["codex"]["state"]
    assert state["pending_units"] == {} and state["retry_roots"] == {}


# ---------------------------------------------------------------------------
# Track 106A: discovery finishes across attended pushes and on explicit runs
# ---------------------------------------------------------------------------


def _acknowledged_codex(home: Path) -> set[str]:
    """Rollouts the persisted Codex index has acknowledged (complete or pending)."""
    record = events.read_git_capture_cache()["probe_index"]["codex"]
    acknowledged: set[str] = set()
    sessions = home / ".codex" / "sessions"
    for key in record["units"]:
        for path in (sessions / key.removeprefix("live/")).glob("rollout-*.jsonl"):
            acknowledged.add(f"{key}/{path.name}")
    for key, progress in record["state"].get("pending_units", {}).items():
        after = progress.get("reader", {}).get("after")
        if after is None:
            continue
        day = sessions / key.removeprefix("live/")
        acknowledged.update(
            f"{key}/{path.name}" for path in day.glob("rollout-*.jsonl") if path.name <= after
        )
    return acknowledged


def test_budgeted_pushes_finish_a_cold_index_and_never_reset_a_finished_prober(home, monkeypatch):
    import dataclasses

    repo_f = _fake_repo(home / "repo-f")
    repo_c = _fake_repo(home / "repo-c")
    projects = home / ".paseo" / "projects" / "projects.json"
    projects.parent.mkdir(parents=True)
    projects.write_text(json.dumps([{"rootPath": str(repo_f)}]))
    for day in ("2026/11/01", "2026/11/02", "2026/11/03"):
        for i in range(4):
            _codex_rollout(_codex_day(home, day) / f"rollout-{i}.jsonl", str(repo_c))

    clock = [1000.0]
    paseo_reads: list[str] = []
    codex_reads: list[str] = []
    original_cwd = rootprobes._codex_rollout_cwd

    def read_rollout(path):
        codex_reads.append(f"{path.parent.name}/{path.name}")
        clock[0] += 1.0
        return original_cwd(path)

    paseo = next(p for p in rootprobes.PROBERS if p.name == "paseo")
    codex = next(p for p in rootprobes.PROBERS if p.name == "codex")

    def counting_read(home_, key, deadline):
        paseo_reads.append(key)
        return paseo.read_unit(home_, key, deadline)

    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rootprobes, "_codex_rollout_cwd", read_rollout)
    monkeypatch.setattr(
        rootprobes, "PROBERS", (dataclasses.replace(paseo, read_unit=counting_read), codex)
    )

    seen: set[str] = set()
    paseo_snapshot = None
    complete_on = None
    for push in range(1, 7):
        codex_reads.clear()
        result = _discover(_cfg("codex"), persist=True, seconds=4.5)
        assert repo_f in result.roots, "a finished prober's root survives every push"
        assert len(codex_reads) == len(set(codex_reads)), "no rollout is read twice in one push"
        cache = events.read_git_capture_cache()
        paseo_units = cache["probe_index"]["paseo"]["units"]
        paseo_snapshot = paseo_snapshot or json.loads(json.dumps(paseo_units))
        assert paseo_units == paseo_snapshot and len(paseo_reads) == 1
        acknowledged = _acknowledged_codex(home)
        if not result.exceeded:
            complete_on = push
            assert cache["prober_resume_after"] is None
            assert repo_c in result.roots
            break
        assert cache["prober_resume_after"] == "codex"
        assert acknowledged > seen, "every push acknowledges more rollouts"
        seen = acknowledged
    assert complete_on == 3, "a cold index spanning more than two budgets ends on push three"


def test_one_explicit_run_completes_discovery_left_with_a_stale_round(home, monkeypatch):
    repo = _fake_repo(home / "repo")
    for day in ("2026/11/01", "2026/11/02", "2026/11/03"):
        for i in range(3):
            _codex_rollout(_codex_day(home, day) / f"rollout-{i}.jsonl", str(repo))
    clock = [0.0]
    original = rootprobes._codex_rollout_cwd
    reads: list[str] = []

    def read(path):
        reads.append(f"{path.parent.name}/{path.name}")
        clock[0] += 1.0
        return original(path)

    monkeypatch.setattr(rootprobes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rootprobes, "_codex_rollout_cwd", read)
    interrupted = _discover(_cfg("codex"), persist=True, seconds=4.5)
    assert interrupted.exceeded
    state = events.read_git_capture_cache()["probe_index"]["codex"]["state"]
    # A stale round: an interrupted day plus a unit the round has yet to reach.
    assert state.get("round_incomplete") and state.get("pending_units") and state["unit_remaining"]
    reads.clear()
    result = _discover(_cfg("codex"), persist=True, seconds=1000.0)
    assert not result.exceeded and result.roots == (repo,)
    assert len(reads) == len(set(reads)), "each rollout is read at most once in the call"
    assert not events.read_git_capture_cache()["probe_index"]["codex"]["state"].get("pending_units")


@pytest.mark.parametrize("persist", [False, True])
def test_recapture_discovers_on_its_own_budget(tmp_path, monkeypatch, persist):
    assert events.RECAPTURE_DISCOVERY_BUDGET_MS >= 30_000
    seen: list[float] = []

    def spy(config, *, deadline_monotonic=None, persist=False, since=None):
        seen.append(deadline_monotonic - time.monotonic())
        return events.GitRootDiscovery((), ())

    monkeypatch.setattr(events, "discover_git_roots", spy)
    sources = [{"name": "mm-events", "path": str(tmp_path / "mm-events")}]
    capture = events_tail._prepare_recapture(
        {}, sources, "dev-a", since=datetime.now(timezone.utc) - timedelta(days=30), persist=persist
    )
    assert capture is not None and len(seen) == 1
    assert seen[0] >= events.RECAPTURE_DISCOVERY_BUDGET_MS / 1000.0 - 1.0


def test_attended_discovery_budget_leaves_autopush_and_legacy_unchanged(
    monkeypatch, tmp_path, home
):
    assert events.ROOT_DISCOVERY_BUDGET_ATTENDED_MS == 1500
    assert events.ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS == 50
    assert events.ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS == 100
    seen: list[int] = []
    real_capture = events_tail._capture_event_snapshots

    def spy(*args, **kwargs):
        seen.append(kwargs["root_discovery_budget_ms"])
        return real_capture(*args, **kwargs)

    monkeypatch.setattr(events_tail, "_capture_event_snapshots", spy)
    monkeypatch.setattr(events_tail, "_attended_host_elapsed_s", None)
    sources = [{"name": "mm-events", "path": str(tmp_path / "mm-events")}]

    def run(config, *, quiet):
        events_tail._run_events_tail(
            config, sources, "dev-a", dry_run=False, quiet=quiet, suppress_host_capture=True
        )
        return seen[-1]

    assert run({}, quiet=False) == events.ROOT_DISCOVERY_BUDGET_ATTENDED_MS
    assert run({}, quiet=True) == events.ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS
    legacy = {"retro": {"host_usage_interactive_budget_ms": 500}}
    assert run(legacy, quiet=False) == events.ROOT_DISCOVERY_BUDGET_INTERACTIVE_MS


def test_status_names_recapture_as_finishing_partial_discovery(tmp_path, monkeypatch, capsys):
    from mind_meld import cli
    from tests.test_track_30a import _write_push

    def status(discovery: str, *, aborts: int, errors: int, streak: int) -> str:
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=3)
        directory = tmp_path / f"{discovery}-{streak}"
        _write_push(
            directory / "events",
            "dev-a",
            ts=now,
            since=since,
            discovery=discovery,
            walk_budget_aborts=aborts,
            walk_errors=errors,
        )
        monkeypatch.setattr(events, "count_pending_repositories", lambda *a: 2)
        monkeypatch.setattr(events, "read_git_capture_cache", lambda: {"incomplete_streak": streak})
        monkeypatch.setattr(
            events,
            "resolve_push_cursor",
            lambda *a: events.CursorResolution(since=since, floored_after_incomplete=False),
        )
        monkeypatch.setattr(events, "effective_capture_since", lambda roots, lower: lower)
        assert cli._print_retro_capture_status(
            [{"name": "mm-events", "path": str(directory)}], "dev-a"
        )
        return " ".join(capsys.readouterr().out.split())

    partial = status("partial", aborts=0, errors=0, streak=3)
    assert "repository discovery has not finished" in partial
    assert (
        "Finish repository discovery and recover this Mac's last 3 days: mm recapture 3d" in partial
    )
    complete = status("complete", aborts=0, errors=1, streak=1)
    assert "mm recapture" in complete and "Finish repository discovery" not in complete


def test_discovery_budget_docs_match_the_constants():
    import re

    root = Path(__file__).resolve().parents[1]

    def section(path: str, heading: str, next_heading: str) -> str:
        text = (root / path).read_text(encoding="utf-8")
        start = text.index(heading)
        end = text.find(next_heading, start + len(heading))
        return " ".join(text[start : end if end != -1 else None].split())

    recapture = f"{events.RECAPTURE_DISCOVERY_BUDGET_MS // 1000} s"
    attended = f"{events.ROOT_DISCOVERY_BUDGET_ATTENDED_MS:,} ms"
    readme_recapture = section(
        "README.md", "### Dropped repositories and ignored git environment variables", "\n### "
    )
    retro_recapture = section(
        "docs/invariants/events-retro.md", "## Cursor gate + recapture", "\n## "
    )
    assert re.search(rf"discovery gets {recapture}\b", readme_recapture)
    assert "keeps its 100 ms" not in readme_recapture
    assert re.search(rf"RECAPTURE_DISCOVERY_BUDGET_MS` \({recapture}\b", retro_recapture)
    readme_upgrade = section("README.md", "## Upgrading", "\n## ")
    retro_budget = section(
        "docs/invariants/events-retro.md", "**Git-root discovery budget and reuse", "\n\n"
    )
    for name, text in (("README.md", readme_upgrade), ("events-retro.md", retro_budget)):
        assert attended in text, f"{name} omits {attended}"
