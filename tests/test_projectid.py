"""Track 90A: Claude project memory shared across machines by git remote.

A ``Machine`` is one simulated Mac or Linux box: its own filesystem root (the
``projectid._fs_root`` seam), home, Claude folder, config and sidecar, all
syncing through one shared encrypted store. Nothing here mocks the codec, the
index, the crypto envelope or the storage: pushes and pulls run
``cli._push_core`` / ``pullflow._pull_core`` end to end.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mind_meld import config as config_module
from mind_meld import (
    core,
    crypto,
    devices,
    fsutil,
    manifest,
    projectid,
    pullflow,
    pullhistory,
    pullplan,
    sidecar,
    statedb,
)
from mind_meld.cli import PushResult, _push_core, app
from mind_meld.errors import ManifestError
from mind_meld.storage import keys as storage_keys
from mind_meld.storage.keys import manifest_key
from mind_meld.storage.local import LocalBackend

runner = CliRunner()
PASSPHRASE = "project-names-test-only"
MEMORY_KB = 1024
GIT_URL = "git@github.com:acme/widgets.git"
IDENTITY = "github.com/acme/widgets"
WIRE = "@git-github.com%2Facme%2Fwidgets"


# ── harness ──────────────────────────────────────────────────────────


class Machine:
    """One machine syncing through a shared store."""

    def __init__(self, tmp_path: Path, store: Path, name: str, home: str, device_id: str):
        self.name = name
        self.device_id = device_id
        self.home_path = "/" + home.strip("/")
        self.fs_root = tmp_path / f"fs-{name}"
        self.home = self.fs_root / home.strip("/")
        self.claude = self.home / ".claude"
        self.claude.mkdir(parents=True)
        self.sidecar_dir = tmp_path / f"sidecar-{name}"
        self.sidecar_dir.mkdir()
        # Each machine keeps its own state.db and recovery folder, as a real one does.
        self.state_dir = tmp_path / f"state-{name}"
        # The config path is pinned for every command: tests/conftest.py gives each distinct
        # config its own state.db, so direct calls and `cli()` must share one.
        self.config_path = self.sidecar_dir / "config.toml"
        self.cwd: Path | None = None  # where its commands run; its home by default
        self.store = store
        self.config = {
            "device": {"id": device_id, "name": name},
            "storage": {"path": str(store)},
            "crypto": {"argon2_memory_kb": MEMORY_KB},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [{"name": "claude", "path": str(self.claude), "type": "claude"}],
            },
        }
        devices.register_device(LocalBackend(store), device_id, name)

    @contextmanager
    def on(self):
        """Run as this machine: its HOME, filesystem root, sidecar and codec context."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("HOME", str(self.home))
            mp.setenv(projectid.WIRE_HOME_TEST_ENV, "1")
            mp.setattr(projectid, "_fs_root", lambda: self.fs_root)
            mp.setattr(sidecar, "SIDECAR_DIR", self.sidecar_dir)
            mp.setattr(statedb, "DB_DIR", self.state_dir)
            mp.setattr(pullhistory, "HISTORY_DIR", self.state_dir)
            config_module.save_config(self.config, self.config_path)
            mp.setattr(config_module, "CONFIG_PATH", self.config_path)
            mp.chdir(self.cwd or self.home)
            core._configure_project_names(self.config)
            try:
                yield self
            finally:
                projectid.reset()

    # -- checkouts and memory

    def checkout(self, rel: str, url: str | None = GIT_URL, *, extra: str = "") -> Path:
        path = self.fs_root / rel.strip("/")
        git = path / ".git"
        git.mkdir(parents=True, exist_ok=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        config = "[core]\n\trepositoryformatversion = 0\n"
        if url is not None:
            config += (
                f'[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
            )
        (git / "config").write_text(config + extra)
        return path

    def folder_for(self, rel: str) -> str:
        return projectid.encode_path("/" + rel.strip("/"))

    def memory(self, folder: str, name: str = "MEMORY.md", text: str | None = None) -> Path:
        path = self.claude / "projects" / folder / "memory" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is not None:
            path.write_text(text)
        return path

    # -- sync

    def push(self, **kwargs) -> PushResult | None:
        with self.on():
            return _push_core(self.config, PASSPHRASE, MEMORY_KB, quiet=True, **kwargs)

    def pull(self, **kwargs):
        with self.on():
            return pullflow._pull_core(self.config, PASSPHRASE, MEMORY_KB, quiet=True, **kwargs)

    def cli(self, *args: str):
        """Run an mm command as this machine, through the real CLI."""
        with self.on(), pytest.MonkeyPatch.context() as mp:
            mp.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
            return runner.invoke(app, list(args))

    def edit_published(self, change) -> None:
        """Rewrite this machine's published manifest in place; ``change`` edits the dict.

        The envelope's first byte (the format) is kept.
        """
        path = Path(self.store) / manifest_key(self.device_id)
        raw = path.read_bytes()
        with self.on():
            data = json.loads(crypto.decrypt_manifest(raw, PASSPHRASE, MEMORY_KB))
            change(data)
            path.write_bytes(
                crypto.encrypt_manifest(
                    json.dumps(data).encode(),
                    PASSPHRASE,
                    MEMORY_KB,
                    wire_names=raw[0] == crypto.MANIFEST_FORMAT_VERSION,
                )
            )

    def claim_base_path(self) -> None:
        """Record the publisher's absolute base path as it would read on a real machine.

        The walker records the real path of this machine's Claude folder, which
        lives under a temporary root here; a real machine's is ``/Users/kb/.claude``.
        The envelope's first byte and the rest of the manifest are untouched.
        """

        def claim(data: dict) -> None:
            data["sources"]["claude"]["base_path"] = "/" + str(
                self.claude.relative_to(self.fs_root)
            )

        self.edit_published(claim)

    def as_the_previous_release(self) -> None:
        """Make the published manifest read as the release before this Track wrote it.

        That release minted deletion records with no content hash, kept no scan-root
        tokens and published no ``from`` ancestry. The sidecar is not touched.
        """

        def strip(data: dict) -> None:
            for record in data.get("tombstones", {}).values():
                record.pop("sha256", None)
            for entry in data["sources"].values():
                entry.pop("root_ids", None)
                for info in entry.get("files", {}).values():
                    info.pop("from", None)

        self.edit_published(strip)

    def published(self, device_id: str | None = None) -> dict:
        """The decrypted bytes of a published manifest, as the wire holds them."""
        raw = LocalBackend(self.store).get(manifest_key(device_id or self.device_id))
        with self.on():
            return json.loads(crypto.decrypt_manifest(raw, PASSPHRASE, MEMORY_KB))

    def first_byte(self, device_id: str | None = None) -> int:
        return LocalBackend(self.store).get(manifest_key(device_id or self.device_id))[0]


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "store"
    backend = LocalBackend(root)
    crypto.bootstrap_crypto_init(backend, PASSPHRASE, MEMORY_KB)
    return root


@pytest.fixture
def mac(tmp_path, store):
    return Machine(tmp_path, store, "mac", "/Users/kb", "aaaaaaaa")


@pytest.fixture
def linux(tmp_path, store):
    return Machine(tmp_path, store, "linux", "/home/kb", "bbbbbbbb")


@pytest.fixture(autouse=True)
def _session(store):
    boot = crypto.fetch_crypto_init(LocalBackend(store))
    crypto.set_crypto_session(boot.root_salt, MEMORY_KB)


def wire_keys(published: dict) -> list[str]:
    return sorted(published["sources"]["claude"]["files"])


# ── remote identity ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:acme/widgets.git", IDENTITY),
        ("https://github.com/acme/widgets", IDENTITY),
        ("https://github.com/acme/widgets.git/", IDENTITY),
        ("https://x-access-token:" + "SECRET@github.com/acme/widgets.git?token=abc", IDENTITY),
        ("ssh://git@github.com/acme/widgets.git", IDENTITY),
        ("ssh://git@github.com:22/acme/widgets.git", IDENTITY),
        ("https://GitHub.com/acme/Widgets.git", "github.com/acme/Widgets"),
        ("ssh://git@host:2222/acme/widgets.git", "host:2222/acme/widgets"),
        ("https://host:8443/acme/widgets", "host:8443/acme/widgets"),
        ("https://host/acme/widgets", "host/acme/widgets"),
        ("alice@host:repo.git", "alice@host/repo"),
        ("bob@host:repo.git", "bob@host/repo"),
        ("alice@host:/srv/repo.git", "alice@host//srv/repo"),
        ("ssh://alice:pw@host/srv/repo.git", "alice@host//srv/repo"),
        ("github.com:acme/widgets", IDENTITY),
        ("host:/srv/widgets.git", "host/srv/widgets"),
        ("git@github.com:acme/widgets.git?token=abc", IDENTITY),
        ("https://github.com/acme/widgets;token=abc", IDENTITY),
        ("https://github.com/acme/100%25.git", "github.com/acme/100%25"),
        ("", None),
        ("/srv/git/widgets.git", None),
        ("../widgets", None),
        ("file:///srv/git/widgets.git", None),
        ("github.com/acme/widgets", None),
        ("C:/Users/kb/widgets", None),
        ("host:", None),
        ("https://[bad/foo", None),
        ("https://[not-an-ip]/foo/bar", None),
        ("https://ex\u2100mple.com/foo", None),
        ("ssh://localhost/repo", None),
        ("git@127.0.0.1:repo.git", None),
        ("http://localhost:3000/me/app", None),
        ("ssh://[::1]/repo", None),
        ("ssh://[2001:db8::1]:2222/foo", "[2001:db8::1]:2222/foo"),
        ("ssh://[2001:db8::1:2222]/foo", "[2001:db8::1:2222]/foo"),
        ("https://[2001:db8::1]/foo.git", "[2001:db8::1]/foo"),
        ("git@[::1]:repo", None),
        ("localhost.:repo", None),
        ("0.0.0.0:repo", None),
        ("ssh://[::ffff:127.0.0.1]/repo", None),
        ("ssh://[0:0:0:0:0:0:0:1]/repo", None),
        ("ssh://[::]/repo", None),
        ("http://2130706433/repo", None),
        ("dev.localhost:repo", None),
        ("127.example.com:acme/widgets", "127.example.com/acme/widgets"),
        ("git@127.1:acme/p", None),
        ("http://0x7f.0.0.1/x", None),
        ("0177.0.0.1:repo", None),
        ("0x7f000001:repo", None),
        ("0.0:repo", None),
        ("git@10.1:acme/p", "10.1/acme/p"),
        ("127.0.0.1.nip.io:acme/p", "127.0.0.1.nip.io/acme/p"),
        ("https://host/org/repo;token=SEC/RET", None),
        ("git@host:org/repo?token=SEC/RET", None),
        ("ssh://git@host/team/a?b/c.git", None),
        ("gcrypt::/mnt/backup/repo.git", None),
        ("hg::/Users/kb/code/repo", None),
        ("ext::foo", None),
        ("alice:secret@host:repo", None),
        ("x-access-token:ghp_TOK@github.com/acme/widgets.git", None),
        ("git@git.example.com:team/c#/alpha.git", "git.example.com/team/c#/alpha"),
        ("ssh://git@git.example.com/team/c#/beta.git", "git.example.com/team/c#/beta"),
        ("https://host/a;x/b.git", None),
        ("https://github.com", None),
        ("https://github.com/", None),
        ("https://host:notaport/acme/widgets", None),
        ("https://host:99999/acme/widgets", None),
        ("https://github.com/acme/wid\x1bgets", None),
        ("ftp://github.com/acme/widgets", None),
        (None, None),
    ],
)
def test_project_identity_table(url, expected):
    assert projectid.project_identity(url) == expected


def test_identity_never_keeps_a_credential():
    # Split: the secret scan reads a literal credential URL as a real one.
    for url in (
        "https://user:" + "hunter2@github.com/acme/widgets.git",
        "ssh://git:hunter2@github.com/acme/widgets.git",
        "https://x-access-token:" + "T0KEN@github.com/acme/widgets",
    ):
        identity = projectid.project_identity(url)
        assert identity and "hunter2" not in identity and "T0KEN" not in identity


def test_scp_git_user_equals_https_but_users_and_ports_differ():
    same = projectid.project_identity
    assert same("git@host:foo/bar") == same("https://host/foo/bar")
    assert same("alice@host:repo.git") != same("bob@host:repo.git")
    assert same("ssh://host:2222/x") != same("ssh://host:3333/x")
    assert same("ssh://git@host:2222/x") != same("https://host/x")


def test_wire_name_is_one_safe_segment():
    wire = projectid.git_wire("host/../x:y@z")
    assert "/" not in wire and ":" not in wire and ".." not in wire.split("/")
    manifest._validate_rel_path(f"projects/{wire}/memory/a.md", where="test")
    assert projectid.wire_identity(wire) == "host/../x:y@z"


def test_display_name_decodes_before_it_sanitizes():
    wire = projectid.git_wire("github.com/acme/" + "\x1b[31mred")
    shown = projectid.display_name(wire)
    assert "\x1b" not in shown
    hostile = "@git-github.com%2Facme%2F%1B%5B31mred"
    assert "\x1b" not in projectid.display_name(hostile)


def test_unplaceable_reads_only_the_name():
    reason = projectid.unplaceable_reason
    assert reason("-Users-kb-widgets") is None
    assert reason(WIRE) == "unplaced"
    assert reason("@old--Users-kb-x") == "old_form"
    assert reason("@home") == "superseded"
    assert reason("@home--x") == "superseded"
    assert reason("@lit--Users-kb-x") == "superseded"
    assert reason("-opt-elsewhere") is None


def test_encode_path_replaces_every_non_alphanumeric():
    assert projectid.encode_path("/Users/kb/Dev/my.proj_x y") == "-Users-kb-Dev-my-proj-x-y"
    assert projectid.encode_path("/Users/kb/.claude") == "-Users-kb--claude"
    assert projectid.encode_path("/Users/kb/😀x") == "-Users-kb--x"


# ── the index ────────────────────────────────────────────────────────


def index_for(machine: Machine):
    with machine.on():
        return projectid.index()


def test_index_main_checkout_owns_its_identity(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, text="x")
    idx = index_for(mac)
    assert idx.owners == {IDENTITY: folder}
    assert idx.decoded == 1 and idx.owned == 1
    assert [r.form for r in idx.rows] == ["git"]


def test_index_decodes_names_with_dots_spaces_and_dashes(mac):
    mac.checkout("/Users/kb/My Dev/wid-gets.v2")
    folder = mac.folder_for("/Users/kb/My Dev/wid-gets.v2")
    mac.memory(folder, text="x")
    assert index_for(mac).owners == {IDENTITY: folder}


def test_index_ambiguous_decode_owns_nothing(mac):
    mac.checkout("/Users/kb/a-b/c")
    mac.checkout("/Users/kb/a_b/c")
    folder = mac.folder_for("/Users/kb/a-b/c")
    mac.memory(folder, text="x")
    idx = index_for(mac)
    assert idx.owners == {}
    assert idx.rows[0].why == projectid.WHY_AMBIGUOUS


@pytest.mark.parametrize(
    "url,why",
    [
        (None, projectid.WHY_NO_ORIGIN),
        ("/srv/git/widgets.git", projectid.WHY_LOCAL_REMOTE),
        ("file:///srv/git/widgets.git", projectid.WHY_LOCAL_REMOTE),
        ("git@localhost:acme/widgets", projectid.WHY_LOCAL_REMOTE),
    ],
)
def test_index_no_network_origin_publishes_home_relative(mac, url, why):
    mac.checkout("/Users/kb/Dev/widgets", url)
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, text="x")
    idx = index_for(mac)
    assert idx.owners == {}
    assert idx.rows[0].why == why and idx.rows[0].form == "home"


def test_index_origin_only_with_several_remotes_and_repeated_fetch(mac):
    mac.checkout(
        "/Users/kb/Dev/widgets",
        GIT_URL,
        extra=(
            '[remote "upstream"]\n\turl = https://github.com/other/fork.git\n'
            "\tfetch = +refs/heads/*:refs/remotes/upstream/*\n"
            "\tfetch = +refs/pull/*/head:refs/remotes/upstream/pr/*\n"
        ),
    )
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, text="x")
    assert index_for(mac).owners == {IDENTITY: folder}


def test_index_percent_in_url_is_kept(mac):
    mac.checkout("/Users/kb/Dev/widgets", "https://github.com/acme/100%25.git")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, text="x")
    assert index_for(mac).owners == {"github.com/acme/100%25": folder}


def test_index_quoted_url_and_include_use_git(mac, monkeypatch):
    calls = []

    def fake_git(checkout):
        calls.append(checkout.name)
        return GIT_URL

    monkeypatch.setattr(projectid, "_git_origin", fake_git)
    mac.checkout("/Users/kb/Dev/quoted", f'"{GIT_URL}"')
    mac.checkout("/Users/kb/Dev/inc", GIT_URL, extra="[include]\n\tpath = ../other\n")
    for rel in ("/Users/kb/Dev/quoted", "/Users/kb/Dev/inc"):
        mac.memory(mac.folder_for(rel), text="x")
    idx = index_for(mac)
    assert sorted(calls) == ["inc", "quoted"]
    assert set(idx.owners) == {IDENTITY}


def test_index_linked_worktree_owns_no_identity_and_nominates_main(mac):
    main = mac.checkout("/Users/kb/Dev/widgets")
    git = main / ".git"
    wt_git = git / "worktrees" / "feature"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feature\n")
    (wt_git / "commondir").write_text("../..\n")
    worktree = mac.fs_root / "Users/kb/wt/feature"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {wt_git}\n")
    wt_folder = mac.folder_for("/Users/kb/wt/feature")
    mac.memory(wt_folder, text="wt")
    idx = index_for(mac)
    # Only the worktree's folder exists: it nominates the main checkout's folder.
    assert idx.owners == {IDENTITY: mac.folder_for("/Users/kb/Dev/widgets")}
    row = next(r for r in idx.rows if r.folder == wt_folder)
    assert row.why == projectid.WHY_WORKTREE and row.form == "home"
    assert idx.to_wire(wt_folder) == "@home-wt-feature"


def test_index_submodule_nominates_nothing(mac):
    super_repo = mac.checkout("/Users/kb/Dev/super")
    modules = super_repo / ".git" / "modules" / "vendor"
    modules.mkdir(parents=True)
    (modules / "HEAD").write_text("ref: refs/heads/main\n")
    sub = mac.fs_root / "Users/kb/Dev/super/vendor"
    sub.mkdir(parents=True)
    (sub / ".git").write_text(f"gitdir: {modules}\n")
    folder = mac.folder_for("/Users/kb/Dev/super/vendor")
    mac.memory(folder, text="x")
    idx = index_for(mac)
    assert idx.owners == {}
    assert idx.rows[0].why == projectid.WHY_SUBMODULE


def test_index_two_clones_choose_memory_then_lexical_owner(mac):
    mac.checkout("/Users/kb/a/widgets")
    mac.checkout("/Users/kb/b/widgets")
    first = mac.folder_for("/Users/kb/a/widgets")
    second = mac.folder_for("/Users/kb/b/widgets")
    mac.memory(second, text="has memory")
    (mac.claude / "projects" / first).mkdir(parents=True)
    idx = index_for(mac)
    assert idx.owners == {IDENTITY: second}
    assert [(c.owner, c.others) for c in idx.clones] == [(second, [first])]
    assert next(r for r in idx.rows if r.folder == first).why == projectid.WHY_SECOND_CLONE


def test_owner_survives_a_second_clone_with_no_memory(mac):
    mac.checkout("/Users/kb/a/widgets")
    owner = mac.folder_for("/Users/kb/a/widgets")
    mac.memory(owner, text="x")
    mac.checkout("/Users/kb/0/widgets")  # lexically smaller, but has no memory
    (mac.claude / "projects" / mac.folder_for("/Users/kb/0/widgets")).mkdir(parents=True)
    assert index_for(mac).owners == {IDENTITY: owner}


def test_index_never_follows_a_symlink_in_the_path(mac):
    real = mac.checkout("/Users/kb/real/widgets")
    link = mac.fs_root / "Users/kb/Dev"
    link.symlink_to(real.parent)
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, text="x")
    idx = index_for(mac)
    assert idx.owners == {}
    assert idx.rows[0].why == projectid.WHY_NO_CHECKOUT


def test_index_rows_name_every_reason(mac):
    mac.checkout("/Users/kb/Dev/ok")
    mac.checkout("/Users/kb/Dev/none", None)
    mac.checkout("/Users/kb/Dev/local", "/srv/x.git")
    mac.checkout("/Users/kb/a-b/c")
    mac.checkout("/Users/kb/a_b/c")
    for rel in (
        "/Users/kb/Dev/ok",
        "/Users/kb/Dev/none",
        "/Users/kb/Dev/local",
        "/Users/kb/a-b/c",
        "/Users/kb/gone/dir",
    ):
        mac.memory(mac.folder_for(rel), text="x")
    whys = {r.folder: r.why for r in index_for(mac).rows}
    assert whys[mac.folder_for("/Users/kb/Dev/ok")] is None
    assert whys[mac.folder_for("/Users/kb/Dev/none")] == projectid.WHY_NO_ORIGIN
    assert whys[mac.folder_for("/Users/kb/Dev/local")] == projectid.WHY_LOCAL_REMOTE
    assert whys[mac.folder_for("/Users/kb/a-b/c")] == projectid.WHY_AMBIGUOUS
    assert whys[mac.folder_for("/Users/kb/gone/dir")] == projectid.WHY_NO_CHECKOUT


def test_index_blocking_scandir_returns_within_the_deadline_and_keeps_identity(mac, monkeypatch):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, text="x")
    mac.config["sync"]["sources"][0]["path"] = str(mac.claude)
    release = threading.Event()
    real = os.scandir

    def blocking(path):
        if Path(path) == mac.fs_root / "Users" / "kb":
            release.wait(10)
        return real(path)

    monkeypatch.setattr(projectid, "INDEX_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(os, "scandir", blocking)
    # The sidecar remembers which identity the folder published under.
    with mac.on():
        projectid.configure(mac.claude, retained={WIRE: folder})
        start = time.monotonic()
        idx = projectid.index()
        elapsed = time.monotonic() - start
        release.set()
    assert elapsed < 2.0
    assert idx.timed_out
    assert idx.owners == {IDENTITY: folder}
    assert idx.to_wire(folder) == WIRE  # no flip to home-relative
    assert next(r for r in idx.rows if r.folder == folder).form == "git"
    time.sleep(0.3)  # a worker finishing late changes nothing
    assert idx.owners == {IDENTITY: folder}


def test_unreadable_checkout_keeps_the_retained_identity_but_a_gone_one_does_not(mac, monkeypatch):
    path = mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, text="x")
    real_read_text = Path.read_text

    def denied(self, *args, **kwargs):
        if self.name == "config" and self.parent.name == ".git":
            raise PermissionError(13, "denied")
        return real_read_text(self, *args, **kwargs)

    with mac.on():
        projectid.configure(mac.claude, retained={WIRE: folder})
        monkeypatch.setattr(Path, "read_text", denied)
        idx = projectid.index()
    assert idx.owners == {IDENTITY: folder}
    assert next(r for r in idx.rows if r.folder == folder).form == "git"
    monkeypatch.undo()
    # A config that is simply gone is not indeterminate: no origin, no identity.
    (path / ".git" / "config").unlink()
    with mac.on():
        projectid.configure(mac.claude, retained={WIRE: folder})
        idx = projectid.index()
    assert idx.owners == {}
    assert idx.rows[0].why == projectid.WHY_NO_ORIGIN


def test_home_unresolved_turns_the_codec_off(mac, monkeypatch):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), text="x")
    with mac.on():
        monkeypatch.setattr(projectid, "_wire_home", lambda: None)
        projectid.configure(mac.claude)
        assert not projectid.enabled() and projectid.index() is None
    mac.push()
    assert mac.first_byte() == crypto.FORMAT_VERSION


def test_pytest_guard_keeps_the_codec_off_without_opt_in(mac, monkeypatch):
    monkeypatch.delenv(projectid.WIRE_HOME_TEST_ENV, raising=False)
    assert projectid._wire_home() is None
    assert projectid.home_failure() is None


def test_diag_state_needs_no_passphrase_and_names_no_peer(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), text="x")
    with mac.on():
        state = projectid.diag_state()
    assert state["folders"] == 1 and state["owned"] == 1 and state["decoded"] == 1
    assert state["rows"][0]["form"] == "git" and state["rows"][0]["why"] is None
    assert state["clones"] == [] and state["timed_out"] is False


# ── the codec at the manifest boundary ───────────────────────────────


def local_manifest(files: dict[str, str], tombstones: dict | None = None) -> dict:
    return {
        "device_id": "d",
        "device_name": "d",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "sources": {
            "claude": {
                "base_path": "/x/.claude",
                "files": {k: {"sha256": v, "size": 1, "mtime": None} for k, v in files.items()},
            },
            "other": {
                "base_path": "/o",
                "files": {"projects/-Users-kb-x/memory/a.md": {"sha256": "9"}},
            },
        },
        "tombstones": tombstones or {},
    }


def test_codec_table(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    owned = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(owned, text="x")
    for extra in (
        "-Users-kb",
        "-Users-kb-loose",
        "-Users-kb--dot",
        "-Users-kb2-x",
        "-opt-elsewhere",
    ):
        (mac.claude / "projects" / extra).mkdir(parents=True)
    keys = {
        f"projects/{owned}/memory/a.md": "1",
        "projects/-Users-kb/memory/home.md": "2",
        "projects/-Users-kb-loose/memory/loose.md": "3",
        "projects/-Users-kb--dot/memory/dot.md": "4",
        "projects/-Users-kb2-x/memory/other-user.md": "5",
        "projects/-opt-elsewhere/memory/outside.md": "6",
        "projects/solo": "7",
        "other/key": "8",
    }
    tomb = {
        f"claude:projects/{owned}/memory/gone.md": {"deleted_at": "2026-01-01T00:00:00+00:00"},
        "claude:projects/-Users-kb-loose/memory/gone.md": {
            "deleted_at": "2026-01-01T00:00:00+00:00"
        },
        "other:projects/-Users-kb-x/memory/gone.md": {"deleted_at": "2026-01-01T00:00:00+00:00"},
    }
    local = local_manifest(keys, tomb)
    before = json.dumps(local, sort_keys=True)
    with mac.on():
        wire = manifest.manifest_to_wire(local)
    assert json.dumps(local, sort_keys=True) == before, "manifest_to_wire must not mutate"
    files = wire["sources"]["claude"]["files"]
    assert sorted(files) == sorted(
        [
            f"projects/{WIRE}/memory/a.md",
            "projects/@home/memory/home.md",
            "projects/@home-loose/memory/loose.md",
            "projects/@home--dot/memory/dot.md",
            "projects/-Users-kb2-x/memory/other-user.md",
            "projects/-opt-elsewhere/memory/outside.md",
            "projects/solo",
            "other/key",
        ]
    )
    assert wire["sources"]["claude"]["wire_folders"] == {WIRE: owned}
    assert wire["sources"]["other"] == local["sources"]["other"], "non-claude sources pass through"
    assert sorted(wire["tombstones"]) == sorted(
        [
            f"claude:projects/{WIRE}/memory/gone.md",
            "claude:projects/@home-loose/memory/gone.md",
            "other:projects/-Users-kb-x/memory/gone.md",
        ]
    )


def test_codec_off_passes_the_manifest_through(mac, monkeypatch):
    local = local_manifest({"projects/-Users-kb-x/memory/a.md": "1"})
    assert manifest.manifest_to_wire(local) is local
    mac.checkout("/Users/kb/Dev/widgets")
    with mac.on():
        monkeypatch.setattr(projectid, "_wire_home", lambda: None)
        projectid.configure(mac.claude)
        assert manifest.manifest_to_wire(local) is local


def test_codec_leaves_a_manifest_without_a_claude_entry_alone(mac):
    local = local_manifest({})
    del local["sources"]["claude"]
    local["tombstones"] = {"claude:projects/-Users-kb-x/memory/a.md": {"deleted_at": "x"}}
    with mac.on():
        wire = manifest.manifest_to_wire(local)
    assert wire is local


def test_a_renamed_claude_type_source_keeps_machine_specific_names(mac):
    local = local_manifest({"projects/-Users-kb-loose/memory/a.md": "1"})
    local["sources"]["work"] = local["sources"].pop("claude")
    with mac.on():
        assert manifest.manifest_to_wire(local) is local


def test_codec_symlinked_home_resolves(tmp_path, store):
    machine = Machine(tmp_path, store, "linked", "/real/kb", "cccccccc")
    alias = machine.fs_root / "Users"
    alias.mkdir()
    (alias / "kb").symlink_to(machine.home)
    with machine.on():
        pass
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(alias / "kb"))
        mp.setenv(projectid.WIRE_HOME_TEST_ENV, "1")
        mp.setattr(projectid, "_fs_root", lambda: machine.fs_root)
        assert projectid.encoded_home() == "-real-kb"


def test_from_wire_round_trip_places_into_the_local_owner(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    owned = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(owned, text="x")
    local = local_manifest(
        {f"projects/{owned}/memory/a.md": "1", "projects/-Users-kb-loose/memory/b.md": "2"}
    )
    with mac.on():
        wire = manifest.manifest_to_wire(local)
        wire["sources"]["claude"]["wire_names"] = True
        loaded = manifest.load_manifest(json.dumps(wire).encode())
    assert sorted(loaded["sources"]["claude"]["files"]) == sorted(
        [f"projects/{owned}/memory/a.md", "projects/-Users-kb-loose/memory/b.md"]
    )


def test_from_wire_marker_must_be_literally_true(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), text="x")
    data = local_manifest({f"projects/{WIRE}/memory/a.md": "1"})
    for marker in ("true", 1, "yes", None):
        data["sources"]["claude"]["wire_names"] = marker
        with mac.on():
            loaded = manifest.load_manifest(json.dumps(data).encode())
        assert list(loaded["sources"]["claude"]["files"]) == [f"projects/{WIRE}/memory/a.md"]


def test_dotdot_in_a_wire_name_is_rejected_at_load(mac):
    data = local_manifest({"projects/@home/../x/memory/a.md": "1"})
    data["sources"]["claude"]["wire_names"] = True
    with mac.on(), pytest.raises(ManifestError):
        manifest.load_manifest(json.dumps(data).encode())


def test_mapping_one_thousand_keys_is_fast(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    owned = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(owned, text="x")
    local = local_manifest({f"projects/{owned}/memory/f{i}.md": str(i) for i in range(1000)})
    with mac.on():
        projectid.index()
        start = time.perf_counter()
        manifest.manifest_to_wire(local)
        assert time.perf_counter() - start < 0.25


# ── two machines through real encrypted storage (AC1, AC2, AC3, AC5) ──


def test_ac5_checkout_at_different_paths_under_mac_and_linux_homes(mac, linux):
    mac.checkout("/Users/kb/Documents/Dev/widgets")
    linux.checkout("/home/kb/code/deep/widgets")
    mac_folder = mac.folder_for("/Users/kb/Documents/Dev/widgets")
    linux_folder = linux.folder_for("/home/kb/code/deep/widgets")
    assert mac_folder != linux_folder
    mac.memory(mac_folder, "MEMORY.md", "- from the mac\n")
    mac.memory(mac_folder, "topic.md", "mac topic\n")
    (linux.claude / "projects" / linux_folder).mkdir(parents=True)

    mac.push()
    published = mac.published()
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    assert published["sources"]["claude"]["wire_names"] is True
    assert wire_keys(published) == [
        f"projects/{WIRE}/memory/MEMORY.md",
        f"projects/{WIRE}/memory/topic.md",
    ]
    assert "-Users-" not in json.dumps(
        {k: v for k, v in published.items() if k != "sources"}
    ) and "-Users-" not in "".join(wire_keys(published))
    assert published["sources"]["claude"]["wire_folders"] == {WIRE: mac_folder}

    result = linux.pull()
    assert result.total_applied == 2
    landed = linux.claude / "projects" / linux_folder / "memory"
    assert (landed / "MEMORY.md").read_text() == "- from the mac\n"
    assert not list((linux.claude / "projects").glob("*Users*"))
    # AC4: the per-project change log lands in the local folder, never under a wire name.
    assert (linux.claude / "projects" / linux_folder / ".mind-meld-log.md").exists()
    assert not list((linux.claude / "projects").glob("@*"))

    # An edit on the Linux checkout returns to the Mac's folder; nothing conflicts.
    (landed / "MEMORY.md").write_text("- from the mac\n- edited on linux\n")
    linux.push()
    assert linux.published()["sources"]["claude"]["wire_folders"] == {WIRE: linux_folder}
    mac.pull()
    assert (
        "- edited on linux"
        in (mac.claude / "projects" / mac_folder / "memory/MEMORY.md").read_text()
    )
    assert not list(mac.claude.rglob("*sync-conflict*"))
    assert not linux.published()["tombstones"] and not mac.published()["tombstones"]


def test_ac2_no_git_remote_publishes_home_relative_and_maps_back(mac, linux):
    mac.memory("-Users-kb-scratch", "note.md", "plain\n")
    mac.memory("-opt-shared", "outside.md", "outside home\n")
    mac.push()
    assert wire_keys(mac.published()) == [
        "projects/-opt-shared/memory/outside.md",
        "projects/@home-scratch/memory/note.md",
    ]
    linux.pull()
    assert (linux.claude / "projects/-home-kb-scratch/memory/note.md").read_text() == "plain\n"
    assert (linux.claude / "projects/-opt-shared/memory/outside.md").exists()
    assert not (linux.claude / "projects/-Users-kb-scratch").exists()
    linux.push()
    assert wire_keys(linux.published()) == wire_keys(mac.published())


def test_unplaced_memory_applies_nothing_mints_nothing_and_places_later(mac, linux):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, "MEMORY.md", "kept\n")
    mac.push()

    result = linux.pull()  # linux has no checkout and no Claude folder for it
    assert result.total_applied == 0
    assert (
        not list((linux.claude / "projects").glob("*"))
        if (linux.claude / "projects").exists()
        else True
    )
    assert result.not_placed_files == 1 and result.not_placed_projects == 1
    linux.push()
    assert not linux.published()["tombstones"]

    clone = linux.checkout("/home/kb/src/widgets")
    linux_folder = linux.folder_for("/home/kb/src/widgets")
    (linux.claude / "projects" / linux_folder).mkdir(parents=True)
    result = linux.pull()
    assert result.total_applied == 1
    assert (linux.claude / "projects" / linux_folder / "memory/MEMORY.md").read_text() == "kept\n"
    assert clone.exists()


def test_current_directory_checkout_places_a_project_nobody_owns(mac, linux, monkeypatch):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "MEMORY.md", "from mac\n")
    mac.push()
    clone = linux.checkout("/home/kb/fresh/widgets")
    (clone / "src").mkdir()
    linux.cwd = clone / "src"
    result = linux.pull()
    folder = linux.folder_for("/home/kb/fresh/widgets")
    assert result.total_applied == 1
    assert (linux.claude / "projects" / folder / "memory/MEMORY.md").read_text() == "from mac\n"
    linux.push()
    assert wire_keys(linux.published()) == [f"projects/{WIRE}/memory/MEMORY.md"]


def test_current_directory_in_a_linked_worktree_places_into_the_main_checkout(
    mac, linux, monkeypatch
):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "MEMORY.md", "from mac\n")
    mac.push()
    main = linux.checkout("/home/kb/main/widgets")
    wt_git = main / ".git" / "worktrees" / "feat"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feat\n")
    (wt_git / "commondir").write_text("../..\n")
    worktree = linux.fs_root / "home/kb/wt/feat"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {wt_git}\n")
    linux.cwd = worktree
    result = linux.pull()
    assert result.total_applied == 1
    assert (
        linux.claude / "projects" / linux.folder_for("/home/kb/main/widgets") / "memory/MEMORY.md"
    ).exists()


def test_current_directory_in_a_submodule_nominates_nothing(mac, linux, monkeypatch):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "MEMORY.md", "from mac\n")
    mac.push()
    super_repo = linux.checkout("/home/kb/super", None)
    modules = super_repo / ".git" / "modules" / "vendor"
    modules.mkdir(parents=True)
    (modules / "HEAD").write_text("ref: refs/heads/main\n")
    sub = linux.fs_root / "home/kb/super/vendor"
    sub.mkdir(parents=True)
    (sub / ".git").write_text(f"gitdir: {modules}\n")
    linux.cwd = sub
    assert linux.pull().total_applied == 0


def test_push_and_status_never_use_the_current_directory(mac, linux, monkeypatch):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "MEMORY.md", "from mac\n")
    mac.push()
    clone = linux.checkout("/home/kb/fresh/widgets")
    linux.cwd = clone
    linux.push()
    with linux.on():
        assert projectid.index().owners == {}


# ── migration: the first push in the shared form (AC3) ───────────────


def seed_old_form(machine: Machine, files: dict[str, str]) -> None:
    """Publish what the previous release published: path names, byte 0x02."""
    for key, text in files.items():
        folder, _, rest = key.partition("/")
        machine.memory(folder, rest.removeprefix("memory/"), text)
    with machine.on():
        projectid.configure(None)  # codec off: the old form
        _push_core(machine.config, PASSPHRASE, MEMORY_KB, quiet=True)
    assert machine.first_byte() == crypto.FORMAT_VERSION
    machine.claim_base_path()


def test_switch_push_with_no_content_change_mints_nothing_and_uploads_nothing(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    seed_old_form(mac, {f"{folder}/memory/MEMORY.md": "a\n", "-Users-kb-loose/memory/b.md": "b\n"})
    backend = LocalBackend(mac.store)
    blobs = sorted(backend.list_keys("data/"))
    with mac.on():
        form = core.project_form_state(
            {"sources": {"claude": {"files": {}}}}, {"sources": {"claude": {}}}
        )
    assert form.switch_due and not form.republish_due

    result = mac.push()
    assert result is not None and result.wire_names_published and not result.content_changed
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    published = mac.published()
    assert published["tombstones"] == {}
    assert sorted(backend.list_keys("data/")) == blobs
    assert wire_keys(published) == sorted(
        ["projects/@home-loose/memory/b.md", f"projects/{WIRE}/memory/MEMORY.md"]
    )
    # The next push has nothing to do.
    assert mac.push() is None
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION


def test_switch_with_the_codec_on_but_no_claude_source_is_never_due(mac):
    cfg = {"sources": {"other": {"files": {}}}}
    with mac.on():
        form = core.project_form_state(cfg, None)
    assert not form.publish_new and not form.switch_due and not form.republish_due


def test_switch_push_outcome_lines(mac):
    from mind_meld.cli import _sync_success

    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    seed_old_form(mac, {f"{folder}/memory/MEMORY.md": "a\n"})
    # Alone, in place of "no content changes".
    with mac.on():
        out = io.StringIO()
        result = _push_core(
            mac.config,
            PASSPHRASE,
            MEMORY_KB,
            quiet=True,
            attended=True,
            result_observer=lambda r: None,
        )
    assert _sync_success("push", result) == "Push: project memory published under shared names."

    # When the same push also uploads, one line says both.
    seed = Machine(mac.fs_root.parent, mac.store, "again", "/Users/kb", "77777777")
    seed.checkout("/Users/kb/Dev/widgets")
    seed_old_form(seed, {f"{folder}/memory/MEMORY.md": "a\n"})
    seed.memory(folder, "new.md", "n\n")
    with seed.on():
        result = _push_core(
            seed.config,
            PASSPHRASE,
            MEMORY_KB,
            quiet=True,
            attended=True,
            result_observer=lambda r: None,
        )
    assert result.wire_names_published and result.published_content_files == 1
    assert _sync_success("push", result) == (
        "Push: 1 content change published; project memory published under shared names."
    )
    assert _sync_success("push", PushResult(names_updated=True)) == "Push: project names updated."
    del out


def test_dry_run_agrees_and_writes_nothing(mac, capsys):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    seed_old_form(mac, {f"{folder}/memory/MEMORY.md": "a\n"})
    before = LocalBackend(mac.store).get(manifest_key(mac.device_id))
    with mac.on():
        out = io.StringIO()
        from mind_meld import cli

        old = cli.console
        cli.console = Console(file=out, width=200, force_terminal=False, color_system=None)
        try:
            assert _push_core(mac.config, PASSPHRASE, MEMORY_KB, dry_run=True) is None
        finally:
            cli.console = old
    assert "Would refresh manifest (shared project names)" in out.getvalue()
    assert LocalBackend(mac.store).get(manifest_key(mac.device_id)) == before


# ── deletions and tombstones (AC3) ───────────────────────────────────


def test_a_deletion_on_one_home_suppresses_on_the_other(tmp_path, store, mac, linux):
    mac.checkout("/Users/kb/Dev/widgets")
    linux.checkout("/home/kb/src/widgets")
    mac_folder = mac.folder_for("/Users/kb/Dev/widgets")
    linux_folder = linux.folder_for("/home/kb/src/widgets")
    keep = mac.memory(mac_folder, "keep.md", "keep\n")
    doomed = mac.memory(mac_folder, "doomed.md", "doomed\n")
    (linux.claude / "projects" / linux_folder).mkdir(parents=True)
    mac.push()
    linux.pull()
    doomed.unlink()
    mac.push()
    tombstones = mac.published()["tombstones"]
    assert list(tombstones) == [f"claude:projects/{WIRE}/memory/doomed.md"]
    assert "-Users-" not in "".join(tombstones)
    # A third machine still lists the file under the shared name. Without the
    # tombstone decoding to Linux's own folder, this pull would bring it back.
    other = Machine(tmp_path, store, "other", "/home/other", "dddddddd")
    other.checkout("/home/other/w")
    other_folder = other.folder_for("/home/other/w")
    other.memory(other_folder, "doomed.md", "doomed\n")
    other.memory(other_folder, "keep.md", "keep\n")
    other.push()
    (linux.claude / "projects" / linux_folder / "memory/doomed.md").unlink()
    linux.pull()
    assert not (linux.claude / "projects" / linux_folder / "memory/doomed.md").exists()
    assert (linux.claude / "projects" / linux_folder / "memory/keep.md").exists()
    assert keep.exists()


def test_a_remote_change_republishes_once_and_mints_nothing(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, "a.md", "a\n")
    mac.push()
    # The repository moves: a different origin names a different project.
    git_config = mac.fs_root / "Users/kb/Dev/widgets/.git/config"
    git_config.write_text(git_config.read_text().replace("acme/widgets", "acme/renamed"))
    result = mac.push()
    assert result is not None and result.names_updated and not result.content_changed
    assert result.total_deleted == 0 and result.total_new == 0
    published = mac.published()
    assert published["tombstones"] == {}
    assert wire_keys(published) == [
        f"projects/{projectid.git_wire('github.com/acme/renamed')}/memory/a.md"
    ]
    assert mac.push() is None  # once


def test_a_moved_checkout_mints_nothing_and_republishes_the_new_folder(mac, linux):
    mac.checkout("/Users/kb/old/widgets")
    old = mac.folder_for("/Users/kb/old/widgets")
    mac.memory(old, "a.md", "from the old folder\n")
    mac.push()
    assert mac.published()["sources"]["claude"]["wire_folders"] == {WIRE: old}
    linux.pull()  # nothing to place yet, but a peer exists

    # The checkout moves: the old path is gone, a new clone has different memory.
    import shutil

    shutil.rmtree(mac.fs_root / "Users/kb/old")
    mac.checkout("/Users/kb/new/widgets")
    new = mac.folder_for("/Users/kb/new/widgets")
    mac.memory(new, "b.md", "from the new folder\n")
    result = mac.push()
    published = mac.published()
    assert published["tombstones"] == {}
    assert result.total_deleted == 0
    assert wire_keys(published) == [
        f"projects/{WIRE}/memory/b.md",
        "projects/@home-old-widgets/memory/a.md",
    ]
    assert published["sources"]["claude"]["wire_folders"] == {WIRE: new}


def test_a_second_clone_taking_over_mints_nothing(mac):
    mac.checkout("/Users/kb/a/widgets")
    first = mac.folder_for("/Users/kb/a/widgets")
    mac.memory(first, "a.md", "one\n")
    mac.push()
    import shutil

    shutil.rmtree(mac.fs_root / "Users/kb/a")
    mac.checkout("/Users/kb/b/widgets")
    second = mac.folder_for("/Users/kb/b/widgets")
    mac.memory(second, "b.md", "two\n")
    mac.push()
    assert mac.published()["tombstones"] == {}


def test_republish_due_refreshes_wire_folders_on_a_no_op_push(mac):
    import shutil

    mac.checkout("/Users/kb/old/widgets")
    old = mac.folder_for("/Users/kb/old/widgets")
    mac.memory(old, "a.md", "same\n")
    mac.push()
    # The checkout and its memory move together: not one byte changes.
    shutil.rmtree(mac.fs_root / "Users/kb/old")
    mac.checkout("/Users/kb/new/widgets")
    new = mac.folder_for("/Users/kb/new/widgets")
    shutil.move(mac.claude / "projects" / old, mac.claude / "projects" / new)
    result = mac.push()
    assert result is not None and result.names_updated and not result.content_changed
    assert result.total_deleted == 0 and result.total_new == 0
    assert mac.published()["sources"]["claude"]["wire_folders"] == {WIRE: new}
    assert mac.published()["tombstones"] == {}
    assert mac.push() is None


def test_sidecar_recovery_after_a_move_mints_nothing(mac):
    mac.checkout("/Users/kb/old/widgets")
    old = mac.folder_for("/Users/kb/old/widgets")
    mac.memory(old, "a.md", "from the old folder\n")
    mac.push()
    import shutil

    shutil.rmtree(mac.fs_root / "Users/kb/old")
    mac.checkout("/Users/kb/new/widgets")
    new = mac.folder_for("/Users/kb/new/widgets")
    mac.memory(new, "b.md", "from the new folder\n")
    # The own manifest is lost; the sidecar is the prior.
    LocalBackend(mac.store).delete(manifest_key(mac.device_id))
    (Path(mac.store) / "manifests" / mac.device_id).mkdir(parents=True, exist_ok=True)
    (Path(mac.store) / manifest_key(mac.device_id)).write_bytes(b"corrupt")
    mac.push()
    assert mac.published()["tombstones"] == {}
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION


def test_first_convergence_unions_memory_and_mints_nothing(mac, linux):
    mac.checkout("/Users/kb/Dev/widgets")
    linux.checkout("/home/kb/src/widgets")
    mac_folder = mac.folder_for("/Users/kb/Dev/widgets")
    linux_folder = linux.folder_for("/home/kb/src/widgets")
    mac.memory(mac_folder, "MEMORY.md", "- mac line\n")
    mac.memory(mac_folder, "topic.md", "mac topic\n")
    linux.memory(linux_folder, "MEMORY.md", "- linux line\n")
    linux.memory(linux_folder, "topic.md", "linux topic\n")
    mac.push()
    linux.pull()
    landed = linux.claude / "projects" / linux_folder / "memory"
    memory = (landed / "MEMORY.md").read_text()
    assert "- mac line" in memory and "- linux line" in memory
    # The same-named topic file keeps its local bytes; the other side is a sidecar or skipped.
    assert (landed / "topic.md").read_text() == "linux topic\n"
    linux.push()
    mac.pull()
    assert not mac.published()["tombstones"] and not linux.published()["tombstones"]
    # A second round settles: nothing further to apply on either side.
    linux.pull()
    mac.pull()
    assert linux.pull().total_applied == 0
    assert not mac.published()["tombstones"]


# ── identity-owned folders take only identity names ──────────────────


def test_home_form_copy_of_a_real_project_neither_applies_nor_deletes(mac):
    mac.checkout("/Users/kb/foo")
    folder = mac.folder_for("/Users/kb/foo")
    mac.memory(folder, "keep.md", "local\n")
    data = local_manifest(
        {"projects/@home-foo/memory/stale.md": "9"},
        {"claude:projects/@home-foo/memory/keep.md": {"deleted_at": "2026-12-31T00:00:00+00:00"}},
    )
    data["sources"]["claude"]["wire_names"] = True
    with mac.on():
        loaded = manifest.load_manifest(json.dumps(data).encode())
        files = loaded["sources"]["claude"]["files"]
        assert list(files) == ["projects/@home-foo/memory/stale.md"], "kept for gc, never applied"
        filtered = core._filter_excluded_paths(loaded, {}, None, None)
    assert filtered["sources"]["claude"]["files"] == {}
    assert filtered["tombstones"] == {}
    assert (mac.claude / "projects" / folder / "memory/keep.md").read_text() == "local\n"


def test_collision_keeps_every_sha_and_applies_only_the_identity_entry(mac):
    mac.checkout("/Users/kb/foo")
    folder = mac.folder_for("/Users/kb/foo")
    mac.memory(folder, "a.md", "x")
    data = local_manifest(
        {
            f"projects/{projectid.git_wire(IDENTITY)}/memory/a.md": "1",
            f"projects/{folder}/memory/a.md": "2",
        }
    )
    data["sources"]["claude"]["wire_names"] = True
    mac.config["sync"]["sources"][0]["path"] = str(mac.claude)
    with mac.on():
        loaded = manifest.load_manifest(json.dumps(data).encode())
        files = loaded["sources"]["claude"]["files"]
        assert files[f"projects/{folder}/memory/a.md"]["sha256"] == "1"
        assert files[f"projects/@lit-{folder}/memory/a.md"]["sha256"] == "2"
        kept = core._filter_excluded_paths(loaded, {}, None, None)
    assert list(kept["sources"]["claude"]["files"]) == [f"projects/{folder}/memory/a.md"]
    assert {i["sha256"] for i in files.values()} == {"1", "2"}


def test_a_mixed_merge_withholds_the_old_copy_and_keeps_the_marker(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, "a.md", "x")
    marked = local_manifest({f"projects/{WIRE}/memory/a.md": "1"})
    marked["sources"]["claude"]["wire_names"] = True
    marked["timestamp"] = "2026-02-01T00:00:00+00:00"
    old = local_manifest({"projects/-Users-other-thing/memory/z.md": "2"})
    old["sources"]["claude"]["base_path"] = "/Users/other/.claude"
    old["timestamp"] = "2026-01-01T00:00:00+00:00"
    with mac.on():
        a = manifest.load_manifest(json.dumps(marked).encode())
        b = manifest.load_manifest(json.dumps(old).encode())
        merged = core._merge_manifests([a, b])
    entry = merged["sources"]["claude"]
    assert entry["wire_names"] is True
    assert f"projects/{folder}/memory/a.md" in entry["files"]
    assert "projects/@old--Users-other-thing/memory/z.md" in entry["files"]


# ── a peer still on path names ───────────────────────────────────────


def test_old_form_peer_on_another_home_applies_nothing_and_mints_nothing(mac, linux):
    seed_old_form(mac, {"-Users-kb-scratch/memory/note.md": "old form\n"})
    (linux.claude / "projects" / "-home-kb-mine").mkdir(parents=True)
    result = linux.pull()
    assert result.total_applied == 0
    assert [(p.device_name, p.files) for p in result.old_form_peers] == [("mac", 1)]
    assert not (linux.claude / "projects" / "-Users-kb-scratch").exists()
    linux.push()
    assert linux.published()["tombstones"] == {}
    assert "-Users-" not in "".join(wire_keys(linux.published()))
    clause = pullflow.old_form_clause(result)
    assert clause == "1 file from mac waits until it publishes shared names (mm status)"
    assert "waiting for" not in clause
    assert (
        pullflow.old_form_detail(result) == "1 file from mac waits until it publishes shared names"
    )


def test_old_form_same_home_peer_applies(tmp_path, store, mac):
    twin = Machine(tmp_path, store, "twin", "/Users/kb", "dddddddd")
    seed_old_form(mac, {"-Users-kb-scratch/memory/note.md": "same home\n"})
    result = twin.pull()
    assert result.total_applied == 1 and result.old_form_peers == []
    assert (twin.claude / "projects/-Users-kb-scratch/memory/note.md").read_text() == "same home\n"


def test_old_form_peer_text_goes_through_safety(mac, linux):
    seed_old_form(mac, {"-Users-kb-scratch/memory/note.md": "x\n"})
    devices.register_device(LocalBackend(mac.store), mac.device_id, "mac\x1b[31m")
    result = linux.pull()
    assert "\x1b" not in pullflow.old_form_clause(result)


def test_an_old_form_tombstone_removes_no_local_bytes(tmp_path, store, mac):
    twin = Machine(tmp_path, store, "twin", "/Users/kb", "dddddddd")
    seed_old_form(
        mac, {"-Users-kb-scratch/memory/note.md": "x\n", "-Users-kb-scratch/memory/gone.md": "y\n"}
    )
    twin.pull()
    local = twin.claude / "projects/-Users-kb-scratch/memory/note.md"
    twin_gone = twin.claude / "projects/-Users-kb-scratch/memory/gone.md"
    (mac.claude / "projects/-Users-kb-scratch/memory/gone.md").unlink()
    seed_old_form(mac, {})
    # The record a release before the content hash wrote: it names the path and nothing
    # else, so it stops the re-download and can delete no bytes (criteria 9 and 20).
    mac.as_the_previous_release()
    assert all("sha256" not in r for r in mac.published()["tombstones"].values())
    assert mac.published()["tombstones"], "the deletion is recorded"
    third = Machine(tmp_path, store, "third", "/Users/kb", "eeeeeeee")
    seed_old_form(
        third,
        {"-Users-kb-scratch/memory/gone.md": "y\n", "-Users-kb-scratch/memory/more.md": "z\n"},
    )
    twin.pull()
    # The tombstone removes no local bytes, and it suppresses only its own path.
    assert twin_gone.read_text() == "y\n" and local.read_text() == "x\n"
    twin_gone.unlink()
    twin.pull()
    assert not twin_gone.exists()
    assert (twin.claude / "projects/-Users-kb-scratch/memory/more.md").read_text() == "z\n"


# ── history: one row per peer ────────────────────────────────────────


def test_unplaceable_names_write_one_history_row_per_peer(mac, linux):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    for i in range(300):
        mac.memory(folder, f"f{i}.md", f"{i}\n")
    mac.push()
    from mind_meld import pullhistory

    rows = []
    real_append = pullhistory.append

    def spy(**kwargs):
        rows.append(kwargs)
        return real_append(**kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pullhistory, "append", spy)
        with linux.on():
            out = io.StringIO()
            mp.setattr(
                pullflow,
                "console",
                Console(file=out, width=200, force_terminal=False, color_system=None),
            )
            pullflow._pull_core(linux.config, PASSPHRASE, MEMORY_KB, quiet=False)
    excluded = [r for r in rows if r.get("action") == "excluded"]
    assert len(excluded) == 1 and excluded[0]["device"] == mac.device_id


# ── the preview list ─────────────────────────────────────────────────


def test_pull_dry_run_verbose_lists_not_placed_projects_and_writes_nothing(mac, linux):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "MEMORY.md", "x\n")
    mac.push()
    out = io.StringIO()
    before = sorted(p for p in linux.claude.rglob("*"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            pullflow,
            "console",
            Console(file=out, width=200, force_terminal=False, color_system=None),
        )
        with linux.on():
            pullflow._pull_core(
                linux.config, PASSPHRASE, MEMORY_KB, dry_run=True, verbose=True, quiet=False
            )
    text = out.getvalue()
    assert text.splitlines()[0] == "This directory: no identity (no_checkout)" or text.splitlines()[
        0
    ].startswith("This directory:")
    assert "Not placed here:" in text
    assert f"{IDENTITY}: 1 file from mac. Have a checkout? Run mm pull inside it." in text
    assert sorted(p for p in linux.claude.rglob("*")) == before


def test_pull_dry_run_verbose_lists_clones_and_superseded(mac, tmp_path, store):
    mac.checkout("/Users/kb/a/widgets")
    mac.checkout("/Users/kb/b/widgets")
    first = mac.folder_for("/Users/kb/a/widgets")
    second = mac.folder_for("/Users/kb/b/widgets")
    mac.memory(first, "a.md", "x")
    mac.memory(second, "b.md", "x")
    peer = Machine(tmp_path, store, "peer", "/Users/kb", "eeeeeeee")
    peer.memory("-Users-kb-a-widgets", "stale.md", "stale\n")
    peer.push()  # a stale home-form copy of a project real on `mac`
    out = io.StringIO()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            pullflow,
            "console",
            Console(file=out, width=200, force_terminal=False, color_system=None),
        )
        with mac.on():
            pullflow._pull_core(
                mac.config, PASSPHRASE, MEMORY_KB, dry_run=True, verbose=True, quiet=False
            )
    text = out.getvalue()
    assert f"{IDENTITY} -> {first} (other clone: {second})" in text
    assert "Not applied, this project has a git remote here:" in text
    assert "@home-a-widgets: 1 file from peer" in text


# ── readers and the format byte ──────────────────────────────────────


def test_the_marker_is_set_exactly_when_the_byte_is_0x03(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    mac.push()
    marked = mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    assert marked == (mac.published()["sources"]["claude"].get("wire_names") is True)
    seed = Machine(mac.fs_root.parent, mac.store, "plain", "/Users/kb", "ffffffff")
    seed_old_form(seed, {"-Users-kb-x/memory/a.md": "x\n"})
    assert seed.first_byte() == crypto.FORMAT_VERSION
    assert "wire_names" not in seed.published()["sources"]["claude"]


def test_a_0x03_canonical_beside_a_0x02_copy_reads_the_canonical(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, "a.md", "x\n")
    mac.push()
    backend = LocalBackend(mac.store)
    key = manifest_key(mac.device_id)
    with mac.on():
        old_plain = json.dumps(local_manifest({f"projects/{folder}/memory/old.md": "7"})).encode()
        old_plain = json.dumps({**json.loads(old_plain), "device_id": mac.device_id}).encode()
        copy = crypto.encrypt_manifest(old_plain, PASSPHRASE, MEMORY_KB, wire_names=False)
    (Path(mac.store) / "manifests" / mac.device_id / "manifest.json 2.enc").write_bytes(copy)
    with mac.on():
        fetch = core._fetch_remote_manifest(backend, mac.device_id, PASSPHRASE, MEMORY_KB)
    assert fetch.is_ok and not fetch.conflict_copies_only
    assert fetch.manifest["sources"]["claude"]["wire_names"] is True
    assert backend.get(key)[0] == crypto.MANIFEST_FORMAT_VERSION


def test_a_0x03_copy_alone_is_readable_and_a_0x04_manifest_is_a_newer_format(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    mac.push()
    backend = LocalBackend(mac.store)
    path = Path(mac.store) / manifest_key(mac.device_id)
    raw = path.read_bytes()
    copy = path.with_name("manifest.json 2.enc")
    copy.write_bytes(raw)
    path.unlink()
    with mac.on():
        fetch = core._fetch_remote_manifest(backend, mac.device_id, PASSPHRASE, MEMORY_KB)
    assert fetch.is_ok and fetch.conflict_copies_only
    path.write_bytes(b"\x04" + raw[1:])
    with mac.on():
        fetch = core._fetch_remote_manifest(backend, mac.device_id, PASSPHRASE, MEMORY_KB)
    assert fetch.status == "corrupt" and fetch.newer_version == 4


def test_a_0x04_peer_prints_the_update_remedy_and_exits_zero(mac, linux, capsys):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    mac.push()
    path = Path(mac.store) / manifest_key(mac.device_id)
    path.write_bytes(b"\x04" + path.read_bytes()[1:])
    result = linux.pull()
    err = " ".join(capsys.readouterr().err.split())
    assert result.corrupt_peer_count == 1
    assert "newer mm (format 0x04)" in err and "Upgrade with:" in err
    assert "is corrupt - skipping" not in err


def test_a_0x03_blob_is_refused():
    blob = crypto.encrypt(b"x", PASSPHRASE, MEMORY_KB)
    with pytest.raises(Exception, match="0x03"):
        crypto.decrypt(b"\x03" + blob[1:], PASSPHRASE, MEMORY_KB)
    assert crypto.decrypt_manifest(b"\x03" + blob[1:], PASSPHRASE, MEMORY_KB) == b"x"
    assert crypto.newer_manifest_version(b"\x03") is None
    assert crypto.newer_manifest_version(b"\x04") == 4
    assert crypto.newer_manifest_version(b"\x0f") == 15
    assert crypto.newer_manifest_version(b"\x10") is None


def test_the_gc_late_recheck_reads_the_new_byte(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    mac.push()
    backend = LocalBackend(mac.store)
    with mac.on():
        fetch = core._fetch_remote_manifest(backend, mac.device_id, PASSPHRASE, MEMORY_KB)
    assert fetch.is_ok and fetch.manifest["sources"]["claude"]["files"]


# ── no-claude-source machines ────────────────────────────────────────


def test_a_machine_without_a_claude_source_publishes_the_old_form(tmp_path, store):
    machine = Machine(tmp_path, store, "bare", "/Users/kb", "99999999")
    notes = machine.home / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("x\n")
    machine.config["sync"]["sources"] = [
        {"name": "notes", "type": "generic", "path": str(notes), "include_dirs": ["."]}
    ]
    machine.push()
    assert machine.first_byte() == crypto.FORMAT_VERSION


# ── mm status, mm diag (Track 90A output contract) ───────────────────


def first_line(result) -> str:
    return result.output.splitlines()[0]


def test_status_ready_to_publish_shared_names_then_silent(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    seed_old_form(mac, {f"{folder}/memory/MEMORY.md": "a\n"})
    result = mac.cli("status")
    assert result.exit_code == 0, result.output
    assert first_line(result) == "Project memory ready to publish under shared names. Run: mm push"
    assert "Metadata-only" not in result.output  # no mtime line in the body
    mac.push()
    result = mac.cli("status")
    assert first_line(result) == "In sync with this device's last push."
    assert "Not placed here" not in result.output and "shared names" not in result.output


def test_status_never_masks_a_conflict_or_content_counts_with_the_names_verdict(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    seed_old_form(mac, {f"{folder}/memory/MEMORY.md": "a\n"})
    mac.memory(folder, "new.md", "n\n")
    result = mac.cli("status")
    assert "1 new, 0 modified and 0 deleted waiting to push" in first_line(result)
    (
        mac.claude
        / "projects"
        / folder
        / "memory"
        / "x.sync-conflict-20260101-000000-v1-bbbbbbbb.md"
    ).write_text("c")
    from mind_meld import statusflow

    idle = {"selected": None, "unknown_reason": None, "unresolved": False}

    def verdict(**kw):
        return statusflow._status_verdict(
            **{
                "source": None,
                "blocking": None,
                "conflicts": 0,
                "projection": {"pull": idle, "push": idle},
                "counts": (0, 0, 0),
                "metadata": True,
                "first_push": False,
                "names_ready": True,
                **kw,
            }
        )

    ready = "Project memory ready to publish under shared names. Run: mm push"
    assert verdict() == ready
    assert verdict(conflicts=1).startswith("1 unresolved conflicts")
    assert "1 new" in verdict(counts=(1, 0, 0))
    stuck = {"selected": {"cause": "degraded"}, "unknown_reason": None, "unresolved": True}
    assert "needs attention" in verdict(projection={"pull": stuck, "push": idle})
    # A hold names itself only when the owner has nothing to run.
    held = {"names_ready": False, "names_held": True}
    assert verdict(**held).startswith("Metadata-only changes waiting to push")
    assert verdict(**held, metadata=False).startswith("Project memory not published until")
    assert "1 new" in verdict(**held, metadata=False, counts=(1, 0, 0))
    # And through the command: a real conflict outranks the ready verdict.
    (mac.claude / "projects" / folder / "memory/new.md").unlink()
    assert first_line(mac.cli("status")).startswith("1 unresolved conflicts")


def test_status_source_other_prints_no_project_name_text(mac, linux):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    notes = mac.home / "notes-src"
    notes.mkdir()
    (notes / "n.md").write_text("n\n")
    mac.config["sync"]["sources"].append({"name": "notes", "path": str(notes), "type": "generic"})
    seed_old_form(mac, {f"{folder}/memory/MEMORY.md": "a\n"})
    # A peer on another home has names withheld, so an ungated status would print them.
    seed_old_form(linux, {"-home-kb-scratch/memory/note.md": "x\n"})
    plain = mac.cli("status")
    assert "until it publishes shared names" in plain.output
    result = mac.cli("status", "--source", "notes")
    assert result.exit_code == 0, result.output
    assert first_line(result).startswith("Source 'notes'")
    assert "shared names" not in result.output and "Not placed here" not in result.output


def test_status_not_placed_here_counts_once_across_peers(mac, linux, tmp_path, store):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "a\n")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "b.md", "b\n")
    mac.push()
    third = Machine(tmp_path, store, "third", "/Users/kb", "99999999")
    third.checkout("/Users/kb/elsewhere/widgets")
    third.memory(third.folder_for("/Users/kb/elsewhere/widgets"), "a.md", "a\n")  # same subpath
    third.push()
    result = linux.cli("status")
    assert "Not placed here: 2 files in 1 project with no known checkout" in result.output
    assert "(list: mm pull --dry-run --verbose)" in result.output
    assert linux.cli("status").output == result.output  # the same inside any directory


def test_status_old_form_row_names_the_machine_and_its_remedy(mac, linux):
    seed_old_form(mac, {"-Users-kb-scratch/memory/note.md": "x\n"})
    result = linux.cli("status")
    lines = result.output.splitlines()
    row = next(line for line in lines if "until it publishes shared names" in line)
    assert row.startswith("1 file from mac waits until it publishes shared names. On mac (")
    assert "mm update, then mm push" in row
    assert any(line.startswith("If one of these machines is gone for good: ") for line in lines)
    assert "https://github.com/kbitz/mind-meld#retiring-a-dead-machine" in result.output


def test_status_old_form_row_below_1_3_0_or_unknown_names_the_install_command(mac, linux):
    from mind_meld import upgrade

    seed_old_form(mac, {"-Users-kb-scratch/memory/note.md": "x\n"})
    backend = LocalBackend(mac.store)
    record = json.loads(backend.get(f"devices/{mac.device_id}.json"))
    record["last_seen_version"] = "0.12.13"
    backend.put(f"devices/{mac.device_id}.json", json.dumps(record).encode())
    row = next(
        line for line in linux.cli("status").output.splitlines() if "until it publishes" in line
    )
    assert "(0.12.13, last push" in row and f"{upgrade.INSTALL_CMD}, then mm push" in row
    del record["last_seen_version"]
    backend.put(f"devices/{mac.device_id}.json", json.dumps(record).encode())
    row = next(
        line for line in linux.cli("status").output.splitlines() if "until it publishes" in line
    )
    assert "On mac (version unknown): " + upgrade.INSTALL_CMD in row


def test_status_prints_no_row_for_a_same_home_older_peer_or_a_vanished_one(
    mac, tmp_path, store, monkeypatch
):
    twin = Machine(tmp_path, store, "twin", "/Users/kb", "dddddddd")
    seed_old_form(mac, {"-Users-kb-scratch/memory/note.md": "x\n"})
    assert "until it publishes" not in twin.cli("status").output
    other = Machine(tmp_path, store, "far", "/home/far", "eeeeeeee")
    monkeypatch.setattr(
        core,
        "_fetch_remote_manifest",
        lambda *a, **k: core.ManifestFetch(status="missing", vanished=True),
    )
    assert "until it publishes" not in other.cli("status").output


def test_status_home_and_encoding_texts(mac, monkeypatch):
    mac.memory("-Users-kb-ghost", "a.md", "x\n")
    (mac.claude / "projects" / "-Users-kb-ghost" / "session.jsonl").write_text("{}\n")
    result = mac.cli("status")
    assert (
        "Project memory syncs by path: no Claude project folder here matches a directory on this "
        "machine, so Claude's folder naming may have changed. Sync continues and nothing is lost. "
        "Run: mm update; if it stays, send the output of ls ~/.claude/projects."
    ) in " ".join(result.output.split())
    with mac.on():
        monkeypatch.setattr(projectid, "home_failure", lambda: "RuntimeError")
        from mind_meld import statusflow

        lines = statusflow._project_name_lines(
            LocalBackend(mac.store), [], mac.device_id, PASSPHRASE, MEMORY_KB
        )
    assert lines == [
        "Project memory syncs by path: could not resolve this machine's home folder "
        "(RuntimeError). Sync continues. Check $HOME, then mm diag."
    ]


def test_encoding_warning_is_silent_when_folders_have_no_session_files(mac, linux):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    mac.push()
    (linux.claude / "projects" / "-home-kb-peer-placed").mkdir(parents=True)
    assert "naming may have changed" not in linux.cli("status").output


def test_diag_json_project_index_schema_needs_no_passphrase_and_no_peer_id(mac, linux):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    mac.push()
    seed_old_form(linux, {"-home-kb-notes/memory/n.md": "n\n"})
    result = mac.cli("diag", "--json")
    assert result.exit_code == 0, result.output
    index = json.loads(result.stdout)["project_index"]
    assert set(index) == {
        "state",
        "reason",
        "folders",
        "decoded",
        "owned",
        "timed_out",
        "clones",
        "rows",
        "machines",
    }
    assert index["state"] == "shared" and index["reason"] is None
    assert index["owned"] == 1 and index["decoded"] == 1 and index["timed_out"] is False
    assert index["rows"][0]["form"] == "git" and index["rows"][0]["identity"] == IDENTITY
    assert index["machines"] == [
        {"name": "linux", "version": index["machines"][0]["version"], "form": "path"}
    ]
    assert linux.device_id not in result.stdout  # a peer id never appears
    assert PASSPHRASE not in result.stdout


def test_diag_state_is_path_before_the_switch_and_defensive_on_failure(mac, monkeypatch):
    seed_old_form(mac, {"-Users-kb-x/memory/a.md": "x\n"})
    assert json.loads(mac.cli("diag", "--json").stdout)["project_index"]["state"] == "path"
    with mac.on():
        monkeypatch.setattr(projectid, "diag_state", lambda: 1 / 0)
        index = json.loads(mac.cli("diag", "--json").stdout)["project_index"]
    assert index["rows"][0]["why"] == "unreadable" and index["state"] is None


def test_diag_reports_config_invalid_without_a_valid_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "missing.toml")
    result = runner.invoke(app, ["diag", "--json"])
    assert json.loads(result.stdout)["project_index"]["reason"] == "config_invalid"


# ── a rolling update: no fleet gate ──────────────────────────────────


@pytest.mark.parametrize("entry", ["older", "no-version", "never-pushed", "unreadable", "vanished"])
def test_h1_the_new_form_publishes_beside_any_kind_of_peer(mac, entry):
    backend = LocalBackend(mac.store)
    key = "devices/cccccccc.json"
    base = {"device_id": "cccccccc", "device_name": "peer", "registered": "2026-01-01T00:00:00Z"}
    if entry == "older":
        backend.put(
            key,
            json.dumps(
                {**base, "last_seen": "2026-08-11T00:00:00Z", "last_seen_version": "0.12.13"}
            ).encode(),
        )
    elif entry == "no-version":
        backend.put(key, json.dumps({**base, "last_seen": "2026-08-11T00:00:00Z"}).encode())
    elif entry == "never-pushed":
        backend.put(key, json.dumps(base).encode())
    elif entry == "unreadable":
        backend.put(key, b"{broken")
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    mac.push()
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION


def test_h2_two_updated_machines_sync_while_an_older_one_stays_silent(tmp_path, store, mac, linux):
    backend = LocalBackend(mac.store)
    older = Machine(tmp_path, store, "old-mac", "/Users/old", "cccccccc")
    seed_old_form(older, {"-Users-old-scratch/memory/note.md": "old\n"})
    before = {k: backend.get(k) for k in backend.list_keys("manifests/cccccccc/")}
    before |= {k: backend.get(k) for k in backend.list_keys("data/cccccccc/")}
    assert before
    silent = {
        "device_id": "cccccccc",
        "device_name": "old-mac",
        "registered": "2026-01-01T00:00:00Z",
        "last_seen": "2026-08-11T00:00:00Z",
        "last_seen_version": "0.12.13",
    }
    backend.put("devices/cccccccc.json", json.dumps(silent).encode())
    mac.checkout("/Users/kb/Dev/widgets")
    linux.checkout("/home/kb/src/widgets")
    mac_folder = mac.folder_for("/Users/kb/Dev/widgets")
    linux_folder = linux.folder_for("/home/kb/src/widgets")
    mac.memory(mac_folder, "MEMORY.md", "- mac\n")
    (linux.claude / "projects" / linux_folder).mkdir(parents=True)
    mac.push()
    linux.pull()
    assert "- mac" in (linux.claude / "projects" / linux_folder / "memory/MEMORY.md").read_text()
    linux.memory(linux_folder, "MEMORY.md", "- mac\n- linux\n")
    linux.push()
    mac.pull()
    assert "- linux" in (mac.claude / "projects" / mac_folder / "memory/MEMORY.md").read_text()
    assert not mac.published()["tombstones"] and not linux.published()["tombstones"]
    after = {k: backend.get(k) for k in backend.list_keys("manifests/cccccccc/")}
    after |= {k: backend.get(k) for k in backend.list_keys("data/cccccccc/")}
    assert after == before  # the silent machine's manifest and blobs are byte-identical


def test_h3_one_machine_at_a_time(tmp_path, store, mac, linux):
    third = Machine(tmp_path, store, "third", "/Users/alex", "99999999")
    seed_old_form(mac, {"-Users-kb-notes/memory/m.md": "m\n"})
    seed_old_form(linux, {"-home-kb-notes/memory/l.md": "l\n"})
    seed_old_form(third, {"-Users-alex-notes/memory/t.md": "t\n"})

    def forms() -> dict[str, str | None]:
        index = json.loads(mac.cli("diag", "--json").stdout)["project_index"]
        return {m["name"]: m["form"] for m in index["machines"]}

    # 1. everyone on the old form: another home's files wait, nothing is applied.
    first = mac.pull()
    assert first.total_applied == 0
    assert sorted((p.device_name, p.files) for p in first.old_form_peers) == [
        ("linux", 1),
        ("third", 1),
    ]
    assert forms() == {"linux": "path", "third": "path"}

    # 2. the Mac switches.
    mac.push()
    assert mac.published()["tombstones"] == {}

    # 3. Linux reads the Mac at once; the third machine's files still wait.
    second = linux.pull()
    assert second.total_applied == 1
    assert [(p.device_name, p.files) for p in second.old_form_peers] == [("third", 1)]
    assert (linux.claude / "projects/-home-kb-notes/memory/m.md").read_text() == "m\n"
    linux.push()
    assert linux.published()["tombstones"] == {}
    assert forms() == {"linux": "shared", "third": "path"}

    # 4. the last machine switches: its clause clears and it receives what it missed.
    third_pull = third.pull()
    assert third_pull.total_applied == 2 and third_pull.old_form_peers == []
    assert (third.claude / "projects/-Users-alex-notes/memory/m.md").exists()
    assert (third.claude / "projects/-Users-alex-notes/memory/l.md").exists()
    third.push()
    assert third.published()["tombstones"] == {}
    assert forms() == {"linux": "shared", "third": "shared"}

    # 5. the first machine now receives the others', and nothing is withheld.
    last = mac.pull()
    assert last.total_applied == 2 and last.old_form_peers == []
    assert (mac.claude / "projects/-Users-kb-notes/memory/l.md").exists()
    assert (mac.claude / "projects/-Users-kb-notes/memory/t.md").exists()


def test_h4_an_old_form_peer_cannot_undo_an_updated_machines_content(tmp_path, store, mac):
    twin = Machine(tmp_path, store, "twin", "/Users/kb", "dddddddd")
    seed_old_form(mac, {"-Users-kb-scratch/memory/keep.md": "remote old form\n"})
    local = twin.memory("-Users-kb-scratch", "keep.md", "diverged local bytes\n")
    os.utime(local, (1_700_000_000, 1_700_000_000))  # local is the older side
    result = twin.pull()
    assert local.read_text() == "diverged local bytes\n"
    assert result.total_conflicted == 1
    assert list(local.parent.glob("keep*sync-conflict*"))
    # A stale remote against a newer local is skipped, not applied.
    os.utime(local, (4_000_000_000, 4_000_000_000))
    assert twin.pull().total_applied == 0 and local.read_text() == "diverged local bytes\n"


def test_h4_another_home_old_form_tombstones_are_dropped(mac, linux):
    seed_old_form(
        mac, {"-Users-kb-scratch/memory/a.md": "a\n", "-Users-kb-scratch/memory/b.md": "b\n"}
    )
    (mac.claude / "projects/-Users-kb-scratch/memory/b.md").unlink()
    with mac.on():
        projectid.configure(None)
        _push_core(mac.config, PASSPHRASE, MEMORY_KB, quiet=True)
    mac.claim_base_path()
    assert any("b.md" in k for k in mac.published()["tombstones"])
    with linux.on():
        fetch = core._fetch_remote_manifest(
            LocalBackend(mac.store), mac.device_id, PASSPHRASE, MEMORY_KB
        )
        filtered = core._filter_excluded_paths(fetch.manifest, {}, None, None)
    assert filtered["tombstones"] == {}


def test_h5_recovery_publishes_the_new_form_and_mints_nothing(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, "a.md", "x\n")
    mac.push()
    path = Path(mac.store) / manifest_key(mac.device_id)
    path.write_bytes(b"corrupt")
    mac.push()  # sidecar recovery: the marker survives in the sidecar
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    assert mac.published()["tombstones"] == {}
    # No sidecar and a corrupt manifest: recovered from peers, still the new form.
    path.write_bytes(b"corrupt")
    (mac.sidecar_dir / "last-push.json").unlink()
    other = Machine(mac.fs_root.parent, mac.store, "peer", "/home/p", "88888888")
    gone = other.memory("-home-p-x", "z.md", "z\n")
    other.memory("-home-p-x", "y.md", "y\n")
    other.push()
    gone.unlink()
    other.push()  # the peer's tombstone is what peer-fallback recovery carries
    mac.push()
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    # Only the peer's own deletion record is carried; this machine minted none.
    assert {t["device_id"] for t in mac.published()["tombstones"].values()} <= {"88888888"}


def test_h5_recover_abandon_manifest_then_push_publishes_the_new_form(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    mac.push()
    path = Path(mac.store) / manifest_key(mac.device_id)
    path.write_bytes(b"corrupt")
    (mac.sidecar_dir / "last-push.json").unlink()  # recover refuses while a sidecar can do it
    result = mac.cli("recover", "--abandon-manifest", "--yes")
    assert result.exit_code == 0, result.output
    mac.push()
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    assert mac.published()["tombstones"] == {}


# ── remaining output contracts and recovery edges ────────────────────


def test_autopush_switch_leaves_a_breadcrumb_and_prints_nothing(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    seed_old_form(mac, {f"{folder}/memory/MEMORY.md": "a\n"})
    backend = LocalBackend(mac.store)
    orphan = f"data/{mac.device_id}/" + "f" * 64
    backend.put(orphan, b"an orphan a GC would collect")
    blobs = sorted(backend.list_keys("data/"))
    result = mac.cli("autopush")
    assert result.exit_code == 0, result.output
    assert mac.published()["tombstones"] == {}
    assert sorted(backend.list_keys("data/")) == blobs  # no new blob, and no GC ran
    assert (result.output + (result.stderr or "")).strip() == ""
    crumb = json.loads((mac.sidecar_dir / "last-autorun.json").read_text())
    assert crumb["push"]["outcome"] == "success"
    assert crumb["push"]["detail"] == "published under shared names"
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    # Once: the next autopush has nothing to say.
    mac.cli("autopush")
    assert "detail" not in json.loads((mac.sidecar_dir / "last-autorun.json").read_text())["push"]


def test_autopull_breadcrumb_names_an_old_form_peer_without_a_pointer(mac, linux):
    seed_old_form(mac, {"-Users-kb-scratch/memory/note.md": "x\n"})
    result = linux.cli("autopull")
    assert result.exit_code == 0, result.output
    detail = json.loads((linux.sidecar_dir / "last-autorun.json").read_text())["pull"]["detail"]
    assert detail == "1 file from mac waits until it publishes shared names"


def test_a_join_adds_the_not_placed_line_once(mac, linux):
    from mind_meld import cli

    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    mac.push()
    out = io.StringIO()
    config_path = linux.sidecar_dir / "config.toml"
    config_module.save_config(linux.config, config_path)
    with linux.on(), pytest.MonkeyPatch.context() as mp:
        mp.setattr(config_module, "CONFIG_PATH", config_path)
        mp.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        mp.setattr(
            cli, "console", Console(file=out, width=200, force_terminal=False, color_system=None)
        )
        cli._attended_pull(
            from_device=None,
            source=None,
            verbose=False,
            dry_run=False,
            conflict_mode="keep-both",
            passphrase=PASSPHRASE,
            join=True,
        )
    text = out.getvalue()
    assert text.count("Not placed here: 1 file in 1 project with no known checkout") == 1
    # A join run inside a checkout places it instead.
    linux.cwd = linux.checkout("/home/kb/src/widgets")
    with linux.on(), pytest.MonkeyPatch.context() as mp:
        mp.setattr(config_module, "CONFIG_PATH", config_path)
        mp.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        cli._attended_pull(
            from_device=None,
            source=None,
            verbose=False,
            dry_run=False,
            conflict_mode="keep-both",
            passphrase=PASSPHRASE,
            join=True,
        )
    assert (
        linux.claude / "projects" / linux.folder_for("/home/kb/src/widgets") / "memory/a.md"
    ).read_text() == "x\n"


def test_moving_a_device_entry_aside_stops_its_clause_and_moving_it_back_restores_it(mac, linux):
    seed_old_form(mac, {"-Users-kb-scratch/memory/note.md": "x\n"})
    entry = Path(mac.store) / f"devices/{mac.device_id}.json"
    aside = entry.with_name("elsewhere.json.bak")
    entry.rename(aside)
    blobs = sorted(LocalBackend(mac.store).list_keys(f"data/{mac.device_id}/"))
    assert linux.pull().old_form_peers == []
    assert "until it publishes" not in linux.cli("status").output
    # The retired machine's blobs are untouched by this release's own-device GC,
    # which really runs here: Linux has a manifest of its own.
    linux.memory("-home-kb-mine", "own.md", "own\n")
    linux.push()
    assert linux.cli("gc").exit_code == 0
    assert sorted(LocalBackend(mac.store).list_keys(f"data/{mac.device_id}/")) == blobs
    aside.rename(entry)
    assert [p.device_name for p in linux.pull().old_form_peers] == ["mac"]


def test_status_count_is_the_same_inside_and_outside_a_fresh_clone(mac, linux, monkeypatch):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    mac.push()
    outside = linux.cli("status").output
    clone = linux.checkout("/home/kb/fresh/widgets")
    linux.cwd = clone
    inside = linux.cli("status").output
    assert "Not placed here: 1 file in 1 project" in outside
    assert inside == outside


def test_sidecar_recovery_after_a_remote_change_mints_nothing_even_with_conflict_copies(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, "a.md", "a\n")
    mac.push()
    git_config = mac.fs_root / "Users/kb/Dev/widgets/.git/config"
    git_config.write_text(git_config.read_text().replace("acme/widgets", "acme/renamed"))
    path = Path(mac.store) / manifest_key(mac.device_id)
    copy = path.with_name("manifest.json 2.enc")
    copy.write_bytes(path.read_bytes())  # a valid conflict copy beside the canonical
    path.write_bytes(b"corrupt")
    mac.push()
    published = mac.published()
    assert published["tombstones"] == {}
    assert wire_keys(published) == [
        f"projects/{projectid.git_wire('github.com/acme/renamed')}/memory/a.md"
    ]


def test_a_permission_error_on_dot_git_keeps_the_retained_identity(mac, monkeypatch):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, "a.md", "x\n")
    real = os.lstat

    def denied(path, *args, **kwargs):
        if str(path).endswith("widgets/.git"):
            raise PermissionError(13, "denied")
        return real(path, *args, **kwargs)

    with mac.on():
        projectid.configure(mac.claude, retained={WIRE: folder})
        monkeypatch.setattr(os, "lstat", denied)
        idx = projectid.index()
    assert idx.owners == {IDENTITY: folder} and idx.to_wire(folder) == WIRE


def test_a_linked_worktree_walk_permission_error_keeps_the_retained_identity(mac, monkeypatch):
    main = mac.checkout("/Users/kb/Dev/widgets")
    wt_git = main / ".git" / "worktrees" / "feat"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feat\n")
    (wt_git / "commondir").write_text("../..\n")
    worktree = mac.fs_root / "Users/kb/wt/feat"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {wt_git}\n")
    folder = mac.folder_for("/Users/kb/wt/feat")
    mac.memory(folder, "a.md", "x\n")
    real = Path.read_bytes

    def denied(self):
        if self.name == "commondir":
            raise PermissionError(13, "denied")
        return real(self)

    with mac.on():
        projectid.configure(mac.claude, retained={WIRE: folder})
        monkeypatch.setattr(Path, "read_bytes", denied)
        idx = projectid.index()
    assert idx.owners == {
        IDENTITY: folder
    }  # the retained identity stands in for the unreadable walk


def test_a_pre_existing_marked_prior_stays_marked_through_merge_and_sidecar(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    mac.push()
    prior = sidecar.read(mac.device_id) if False else None
    with mac.on():
        prior = sidecar.read(mac.device_id)
        assert prior["sources"]["claude"]["wire_names"] is True
        merged = core._merge_manifests([prior, {**prior, "timestamp": "2000-01-01T00:00:00+00:00"}])
        assert merged["sources"]["claude"]["wire_names"] is True
        assert core.project_form_state(prior, merged) == core.ProjectFormState(True, False, False)


def test_a_push_recovered_from_conflict_copies_only_stays_marked(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    mac.push()
    path = Path(mac.store) / manifest_key(mac.device_id)
    path.with_name("manifest.json 2.enc").write_bytes(path.read_bytes())
    path.unlink()
    mac.push()
    assert (
        mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION and mac.published()["tombstones"] == {}
    )


def test_two_local_folders_sharing_one_wire_name_raise_naming_both(mac, monkeypatch):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "x\n")
    local = local_manifest(
        {"projects/-Users-kb-one/memory/a.md": "1", "projects/-Users-kb-two/memory/a.md": "2"}
    )
    with mac.on():
        idx = projectid.index()
        monkeypatch.setattr(type(idx), "to_wire", lambda self, folder: "@home-same")
        with pytest.raises(ManifestError, match="-Users-kb-one.*-Users-kb-two"):
            manifest.manifest_to_wire(local)


def test_a_moved_checkout_receives_the_peers_copy_on_its_next_pull(mac, linux):
    import shutil

    mac.checkout("/Users/kb/old/widgets")
    old = mac.folder_for("/Users/kb/old/widgets")
    mac.memory(old, "a.md", "from the old folder\n")
    mac.push()
    linux.checkout("/home/kb/src/widgets")
    linux_folder = linux.folder_for("/home/kb/src/widgets")
    linux.memory(linux_folder, "from-linux.md", "peer memory\n")
    linux.push()

    shutil.rmtree(mac.fs_root / "Users/kb/old")
    mac.checkout("/Users/kb/new/widgets")
    new = mac.folder_for("/Users/kb/new/widgets")
    mac.memory(new, "b.md", "from the new folder\n")
    mac.push()
    assert mac.published()["tombstones"] == {}
    result = mac.pull()
    assert result.total_applied >= 1
    assert (mac.claude / "projects" / new / "memory/from-linux.md").read_text() == "peer memory\n"


def test_status_shows_a_rename_as_ready_to_publish_not_as_deletions(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "a\n")
    mac.push()
    git_config = mac.fs_root / "Users/kb/Dev/widgets/.git/config"
    git_config.write_text(git_config.read_text().replace("acme/widgets", "acme/renamed"))
    result = mac.cli("status")
    assert first_line(result) == "Project memory ready to publish under shared names. Run: mm push"
    assert "deleted" not in first_line(result) and "- 1 deleted" not in result.output
    mac.push()
    assert first_line(mac.cli("status")) == "In sync with this device's last push."


# ── review findings: what a real fleet does to the codec ─────────────


def _worktree_of(machine: Machine, main: Path, rel: str) -> str:
    """A linked worktree of ``main`` at ``rel``; returns its Claude folder name."""
    wt_git = main / ".git" / "worktrees" / rel.replace("/", "_")
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feature\n")
    (wt_git / "commondir").write_text("../..\n")
    worktree = machine.fs_root / rel.strip("/")
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {wt_git}\n")
    return machine.folder_for(rel)


def _corrupt_own_manifest(machine: Machine) -> None:
    (Path(machine.store) / manifest_key(machine.device_id)).write_bytes(b"corrupt")


def test_a_malformed_origin_disturbs_no_other_folder(mac, capfd):
    mac.checkout("/Users/kb/aaa", "https://[bad/foo")
    mac.checkout("/Users/kb/zzz", "git@github.com:acme/zzz.git")
    bad, good = mac.folder_for("/Users/kb/aaa"), mac.folder_for("/Users/kb/zzz")
    mac.memory(bad, text="a\n")
    mac.memory(good, text="z\n")
    idx = index_for(mac)
    assert idx.owners == {"github.com/acme/zzz": good}
    assert not idx.timed_out
    assert next(r for r in idx.rows if r.folder == bad).why == projectid.WHY_LOCAL_REMOTE
    assert "Traceback" not in capfd.readouterr().err
    mac.push()
    assert "Traceback" not in capfd.readouterr().err
    assert f"projects/{projectid.git_wire('github.com/acme/zzz')}/memory/MEMORY.md" in wire_keys(
        mac.published()
    )


def test_an_unexpected_read_failure_marks_one_folder_unknown(mac, capfd):
    """A NUL in a ``.git`` file raises ``ValueError``; the worker keeps going."""
    (mac.fs_root / "Users/kb/aaa").mkdir(parents=True)
    (mac.fs_root / "Users/kb/aaa/.git").write_bytes(b"gitdir: /nul\x00byte\n")
    mac.checkout("/Users/kb/zzz", "git@github.com:acme/zzz.git")
    bad, good = mac.folder_for("/Users/kb/aaa"), mac.folder_for("/Users/kb/zzz")
    mac.memory(bad, text="a\n")
    mac.memory(good, text="z\n")
    idx = index_for(mac)
    assert idx.owners == {"github.com/acme/zzz": good}
    assert next(r for r in idx.rows if r.folder == bad).why == projectid.WHY_UNREADABLE
    assert "Traceback" not in capfd.readouterr().err


def test_pull_inside_a_checkout_with_a_malformed_origin_succeeds(mac, linux):
    linux.memory("-home-kb-scratch", "a.md", "a\n")
    linux.push()
    mac.cwd = mac.checkout("/Users/kb/aaa", "https://[bad/foo")
    mac.memory(mac.folder_for("/Users/kb/aaa"), text="a\n")
    mac.push()
    result = mac.cli("pull")
    assert result.exit_code == 0, result.output
    assert "unexpected-error" not in result.output
    assert (mac.claude / "projects/-Users-kb-scratch/memory/a.md").exists()


@pytest.mark.parametrize(
    "config",
    [
        f'[remote "origin"]\n\turl = {GIT_URL} # mirror\n',
        f'[remote "origin"]\n\turl = {GIT_URL} ; mirror\n',
        f"[remote.origin]\n\turl = {GIT_URL}\n",
        f'[REMOTE "origin"]\n\turl = {GIT_URL}\n',
        f'[remote  "origin"]\n\turl = {GIT_URL}\n',
        f'[remote "origin"]\nurl = {GIT_URL}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n',
    ],
)
def test_index_agrees_with_git_on_what_the_parser_misreads(mac, config):
    import subprocess

    path = mac.fs_root / "Users/kb/Dev/widgets"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    with (path / ".git/config").open("a") as fh:
        fh.write(config)
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, text="x\n")
    assert index_for(mac).owners == {IDENTITY: folder}


def test_a_fifo_git_config_is_unknown_not_a_hang(mac):
    path = mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, text="x\n")
    (path / ".git/config").unlink()
    os.mkfifo(path / ".git/config")
    with mac.on():
        projectid.configure(mac.claude, retained={WIRE: folder})
        idx = projectid.index()
    assert not idx.timed_out
    assert idx.owners == {IDENTITY: folder}  # unknown keeps the retained identity


def test_the_current_directory_probe_is_bounded(mac, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(projectid, "INDEX_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(projectid, "_cwd_nomination", lambda root: release.wait(5))
    with mac.on():
        projectid.allow_cwd_candidate()
        start = time.monotonic()
        idx = projectid.index()
        elapsed = time.monotonic() - start
        release.set()
    assert elapsed < 2.0
    assert idx.timed_out and idx.cwd.why == projectid.WHY_TIMED_OUT


def test_the_current_directory_needs_a_real_git_dir(mac):
    fake = mac.fs_root / "Users/kb/fake"
    (fake / ".git").mkdir(parents=True)
    (fake / ".git/config").write_text(f'[remote "origin"]\n\turl = {GIT_URL}\n')
    mac.cwd = fake
    with mac.on():
        projectid.allow_cwd_candidate()
        idx = projectid.index()
    assert idx.owners == {} and idx.cwd.identity is None


def test_own_tombstones_survive_a_checkout_that_goes_away_and_returns(mac, linux):
    import shutil

    checkout = mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    mac.memory(folder, "keep.md", "k\n")
    doomed = mac.memory(folder, "del.md", "d\n")
    mac.push()
    twin = linux.checkout("/home/kb/x/p")
    linux.cwd = twin
    linux.pull()
    linux.push()
    doomed.unlink()
    mac.push()
    key = f"claude:projects/{WIRE}/memory/del.md"
    assert list(mac.published()["tombstones"]) == [key]

    moved = checkout.with_name("proj-renamed")
    shutil.move(checkout, moved)
    result = mac.cli("autopush")
    assert result.exit_code == 0, result.output
    assert list(mac.published()["tombstones"]) == [key]  # not erased while the checkout is away

    shutil.move(moved, checkout)
    mac.push()
    mac.pull()
    assert not doomed.exists()  # the peer still lists it; the deletion holds
    assert list(mac.published()["tombstones"]) == [key]


def _with_second_source(machine: Machine) -> None:
    notes = machine.home / "notes-src"
    notes.mkdir(exist_ok=True)
    (notes / "n.md").write_text("n\n")
    machine.config["sync"]["sources"].append(
        {"name": "notes", "path": str(notes), "type": "generic"}
    )


@pytest.mark.parametrize("push_after_enable", [False, True])
def test_disabling_claude_keeps_its_deletion_records(mac, linux, push_after_enable):
    mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    _with_second_source(mac)
    mac.memory(folder, "keep.md", "k\n")
    doomed = mac.memory(folder, "del.md", "d\n")
    mac.memory("-Users-kb-loose", "gone.md", "g\n")
    mac.memory("-Users-kb-loose", "stay.md", "s\n")
    mac.push()
    linux.cwd = linux.checkout("/home/kb/x/p")
    linux.pull()
    linux.push()
    doomed.unlink()
    (mac.claude / "projects/-Users-kb-loose/memory/gone.md").unlink()
    mac.push()
    keys = set(mac.published()["tombstones"])
    assert keys == {
        f"claude:projects/{WIRE}/memory/del.md",
        "claude:projects/@home-loose/memory/gone.md",
    }

    mac.config["sync"]["disabled_sources"] = ["claude"]
    (mac.home / "notes-src/n.md").write_text("changed\n")
    mac.push()
    assert set(mac.published()["tombstones"]) == keys  # disabling must not undo a deletion

    mac.config["sync"]["disabled_sources"] = []
    if push_after_enable:
        mac.push()  # a carried @home name beside its folder must not collide
        assert set(mac.published()["tombstones"]) == keys
    mac.pull()
    assert not doomed.exists()
    assert not (mac.claude / "projects/-Users-kb-loose/memory/gone.md").exists()


def test_deleting_a_project_folder_with_a_live_checkout_stays_deleted(mac, linux):
    import shutil

    checkout = mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    first = mac.memory(folder, "a.md", "a\n")
    mac.memory(folder, "b.md", "b\n")
    mac.push()
    linux.cwd = linux.checkout("/home/kb/x/p")
    linux.pull()
    linux.push()

    shutil.rmtree(mac.claude / "projects" / folder)
    result = mac.cli("status")
    assert "0 new, 0 modified and 2 deleted waiting to push" in first_line(result)
    pushed = mac.push()
    assert pushed.total_deleted == 2 and not pushed.names_updated
    assert set(mac.published()["tombstones"]) == {
        f"claude:projects/{WIRE}/memory/a.md",
        f"claude:projects/{WIRE}/memory/b.md",
    }
    mac.cwd = checkout
    mac.pull()
    assert not first.exists()  # a reset of a project's memory does not undo itself


def test_a_changed_home_mints_no_tombstone_and_settles(mac):
    import shutil

    mac.memory("-Users-kb-loose", "a.md", "a\n")
    mac.push()
    assert wire_keys(mac.published()) == ["projects/@home-loose/memory/a.md"]
    new_home = mac.fs_root / "Users/renamed"
    shutil.move(mac.home, new_home)
    mac.home = new_home
    mac.claude = new_home / ".claude"
    mac.config["sync"]["sources"][0]["path"] = str(mac.claude)

    result = mac.push()
    assert result is not None and not result.content_changed and result.names_updated
    published = mac.published()
    assert published["tombstones"] == {}
    assert wire_keys(published) == ["projects/-Users-kb-loose/memory/a.md"]
    assert mac.push() is None  # it settles


def test_sidecar_recovery_after_a_folder_moved_with_its_checkout_mints_nothing(mac):
    import shutil

    checkout = mac.checkout("/Users/kb/old/widgets")
    old = mac.folder_for("/Users/kb/old/widgets")
    mac.memory(old, "a.md", "a\n")
    mac.push()
    new_checkout = mac.fs_root / "Users/kb/new/widgets"
    new_checkout.parent.mkdir(parents=True)
    shutil.move(checkout, new_checkout)
    new = mac.folder_for("/Users/kb/new/widgets")
    shutil.move(mac.claude / "projects" / old, mac.claude / "projects" / new)
    _corrupt_own_manifest(mac)
    mac.push()
    assert mac.published()["tombstones"] == {}
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION


def test_sidecar_recovery_when_the_identity_has_no_owner_mints_nothing(mac):
    import shutil

    mac.checkout("/Users/kb/old/widgets")
    old = mac.folder_for("/Users/kb/old/widgets")
    mac.memory(old, "a.md", "a\n")
    mac.push()
    shutil.rmtree(mac.fs_root / "Users/kb/old")
    shutil.rmtree(mac.claude / "projects" / old)
    _corrupt_own_manifest(mac)
    mac.push()
    assert mac.published()["tombstones"] == {}


def test_a_peers_stale_0x02_copy_keeps_no_old_form_row(mac, linux):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, "a.md", "a\n")
    seed_old_form(mac, {"-Users-kb-loose/memory/z.md": "z\n"})
    stale = (Path(mac.store) / manifest_key(mac.device_id)).read_bytes()
    assert mac.push() is not None
    copy = Path(mac.store) / manifest_key(mac.device_id)
    copy.with_name(copy.name.replace(".enc", " 2.enc")).write_bytes(stale)
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    assert linux.pull().old_form_peers == []
    assert "until it publishes" not in linux.cli("status").output


def test_encoding_warning_is_silent_on_an_incomplete_index(mac, monkeypatch):
    mac.memory("-Users-kb-ghost", "a.md", "x\n")
    (mac.claude / "projects/-Users-kb-ghost/session.jsonl").write_text("{}\n")
    real_decode = projectid._decode

    def boom(*args, **kwargs):
        raise ValueError("embedded null byte")

    monkeypatch.setattr(projectid, "_decode", boom)
    with mac.on():
        assert projectid.encoding_suspect() is False
    monkeypatch.setattr(projectid, "_decode", real_decode)
    with mac.on():
        assert projectid.encoding_suspect() is True


def test_two_nominations_of_one_folder_keep_one_owner(mac):
    """``a-b`` and ``a.b`` encode alike: only one of their identities may own that name."""
    one = mac.checkout("/Users/kb/a-b", "git@github.com:acme/one.git")
    two = mac.checkout("/Users/kb/a.b", "git@github.com:acme/two.git")
    mac.memory(_worktree_of(mac, one, "/Users/kb/wt/one"), text="x\n")
    mac.memory(_worktree_of(mac, two, "/Users/kb/wt/two"), text="y\n")
    owners = index_for(mac).owners
    assert len(owners) == 1 and len(set(owners.values())) == 1


def test_two_spellings_of_one_identity_keep_both_shas(mac):
    mac.checkout("/Users/kb/foo")
    folder = mac.folder_for("/Users/kb/foo")
    mac.memory(folder, "a.md", "x")
    data = local_manifest(
        {
            "projects/@git-github.com%2Facme%2Fwidgets/memory/a.md": "1",
            "projects/@git-github.com%2facme%2fwidgets/memory/a.md": "2",
        }
    )
    data["sources"]["claude"]["wire_names"] = True
    with mac.on():
        files = manifest.load_manifest(json.dumps(data).encode())["sources"]["claude"]["files"]
    assert {info["sha256"] for info in files.values()} == {"1", "2"}


@pytest.mark.parametrize("value", [["-Users-kb-keep"], 5])
def test_a_serialized_moved_folders_is_ignored_at_load(mac, value):
    data = local_manifest({"projects/-opt-shared/memory/a.md": "1"})
    data["sources"]["claude"]["wire_names"] = True
    data["sources"]["claude"]["moved_folders"] = value
    data["sources"]["claude"]["base_path"] = os.path.realpath(mac.claude)
    mac.memory("-Users-kb-listed", text="x\n")  # `projects/` lists, and the base matches
    with mac.on():
        loaded = manifest.load_manifest(json.dumps(data).encode())
    assert "moved_folders" not in loaded["sources"]["claude"]


def test_pull_of_another_source_reports_no_project_state(mac, linux):
    seed_old_form(mac, {"-Users-kb-scratch/memory/note.md": "x\n"})
    result = linux.pull(source_filter="notes")
    assert result.old_form_peers == [] and result.not_placed_projects == 0


def test_autopush_switch_keeps_its_trail_beside_other_details(mac, monkeypatch):
    from mind_meld import cli as cli_module

    def fake_push(*args, **kwargs):
        return PushResult(wire_names_published=True, maintenance_failures=1)

    monkeypatch.setattr(cli_module, "_push_core", fake_push)
    mac.memory("-Users-kb-loose", "a.md", "a\n")
    assert mac.cli("autopush").exit_code == 0
    crumb = json.loads((mac.sidecar_dir / "last-autorun.json").read_text())["push"]
    assert crumb["outcome"] == "degraded"
    assert "published under shared names" in crumb["detail"]
    assert "maintenance failure" in crumb["detail"]


def test_the_sidecar_is_read_only_when_the_index_is_built(mac, monkeypatch):
    calls = []
    real_read = sidecar.read

    def spy(device_id):
        calls.append(device_id)
        return real_read(device_id)

    monkeypatch.setattr(sidecar, "read", spy)
    with mac.on():
        core._configure_project_names(mac.config)
        assert calls == []
        projectid.index()
        assert calls == [mac.device_id]


def test_mm_gc_keeps_both_shas_of_a_collision_and_collects_a_real_orphan(mac):
    """(k), through `mm gc`'s real reference set: a displaced entry's blob stays."""
    from mind_meld.storage.keys import blob_key

    mac.checkout("/Users/kb/foo")
    folder = mac.folder_for("/Users/kb/foo")
    mac.memory(folder, "a.md", "x")
    mac.push()
    path = Path(mac.store) / manifest_key(mac.device_id)
    with mac.on():
        data = json.loads(crypto.decrypt_manifest(path.read_bytes(), PASSPHRASE, MEMORY_KB))
    identity_sha, literal_sha, orphan = "1" * 64, "2" * 64, "3" * 64
    data["sources"]["claude"]["files"] = {
        f"projects/{WIRE}/memory/a.md": {"sha256": identity_sha, "size": 1, "mtime": None},
        f"projects/{folder}/memory/a.md": {"sha256": literal_sha, "size": 1, "mtime": None},
    }
    with mac.on():
        path.write_bytes(
            crypto.encrypt_manifest(
                json.dumps(data).encode(), PASSPHRASE, MEMORY_KB, wire_names=True
            )
        )
    backend = LocalBackend(mac.store)
    for sha in (identity_sha, literal_sha, orphan):
        backend.put(blob_key(mac.device_id, sha), b"blob")
    assert mac.cli("gc").exit_code == 0
    assert backend.exists(blob_key(mac.device_id, identity_sha))
    assert backend.exists(blob_key(mac.device_id, literal_sha))
    assert not backend.exists(blob_key(mac.device_id, orphan))


def test_an_unlistable_projects_folder_treats_every_prior_folder_as_moved(mac):
    mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    mac.memory(folder, "a.md", "a\n")
    mac.push()
    with mac.on(), pytest.MonkeyPatch.context() as mp:
        mp.setattr(projectid, "_list_project_folders", lambda projects: None)
        projectid.configure(mac.claude, retained={WIRE: folder})
        idx = projectid.index()
        assert idx.listed is False
        moved = manifest._from_wire_mapping(
            {"-opt-shared", "-opt-only-a-record", "@home-x"},
            {"wire_names": True, "base_path": idx.claude_real},
            idx,
            set(),
            {"-opt-shared", "@home-x"},
        )[1]
    # Only folders that hold files: a deletion record alone would republish forever.
    assert moved == {"-opt-shared", "-Users-kb-x"}


def test_a_missing_projects_folder_then_pull_then_push_restores_and_mints_nothing(mac, linux):
    """The pull recreates `projects/`, so the push must not read the rest as deleted."""
    import shutil

    a = mac.checkout("/Users/kb/a", "git@github.com:acme/a.git")
    mac.checkout("/Users/kb/q", "git@github.com:acme/q.git")
    a_file = mac.memory(mac.folder_for("/Users/kb/a"), "a.md", "a\n")
    q_file = mac.memory(mac.folder_for("/Users/kb/q"), "q.md", "q\n")
    mac.push()
    for name in ("a", "q"):
        linux.checkout(f"/home/kb/{name}", f"git@github.com:acme/{name}.git")
        (linux.claude / "projects" / linux.folder_for(f"/home/kb/{name}")).mkdir(parents=True)
    linux.pull()
    linux.push()
    shutil.rmtree(mac.claude / "projects")
    mac.cwd = a
    mac.pull()
    assert a_file.read_text() == "a\n" and q_file.read_text() == "q\n"
    mac.push()
    assert mac.published()["tombstones"] == {}
    assert mac.pull().total_applied == 0  # nothing is blocked or missing


def test_an_outage_with_only_a_deletion_record_republishes_once(mac):
    import shutil

    mac.memory("-Users-kb-loose", "keep.md", "k\n")
    gone = mac.memory("-Users-kb-loose", "gone.md", "g\n")
    mac.push()
    gone.unlink()
    mac.push()
    assert list(mac.published()["tombstones"]) == ["claude:projects/@home-loose/memory/gone.md"]
    shutil.rmtree(mac.claude / "projects")
    assert mac.push() is not None
    assert mac.push() is None  # it settles while `projects/` stays away
    assert list(mac.published()["tombstones"]) == ["claude:projects/@home-loose/memory/gone.md"]


def test_recovery_with_a_missing_projects_folder_mints_nothing(mac):
    import shutil

    mac.memory("-Users-kb-loose", "b.md", "b\n")
    mac.memory("-opt-shared", "c.md", "c\n")
    mac.push()
    shutil.rmtree(mac.claude / "projects")
    _corrupt_own_manifest(mac)
    mac.push()
    assert mac.published()["tombstones"] == {}


def test_an_unmarked_prior_with_a_missing_projects_folder_mints_nothing(mac):
    entry = {"base_path": os.path.realpath(mac.claude), "files": {}}
    with mac.on():
        moved = manifest._from_wire_mapping(
            {"-Users-kb-loose"}, entry, None, set(), {"-Users-kb-loose"}
        )[1]
    assert moved == {"-Users-kb-loose"}  # `projects/` does not exist on this machine yet
    with mac.on():
        record_only = manifest._from_wire_mapping(
            {"-Users-kb-loose", "-Users-kb-only-a-record"}, entry, None, set(), {"-Users-kb-loose"}
        )[1]
    assert record_only == {"-Users-kb-loose"}  # a record alone would republish forever
    mac.memory("-Users-kb-loose", "b.md", "b\n")
    with mac.on():
        moved = manifest._from_wire_mapping(
            {"-Users-kb-loose"}, entry, None, set(), {"-Users-kb-loose"}
        )[1]
    assert moved == set()


def test_a_deleted_folder_past_the_deadline_owns_nothing(mac, monkeypatch):
    """Out of time is no evidence: a live clone may be waiting behind the deadline."""
    mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    other = mac.folder_for("/Users/kb/aaa")
    mac.memory(other, "x.md", "x\n")
    release = threading.Event()
    real_resolve = projectid._resolve_folder

    def slow(name, projects, root, memo):
        if name == other:
            release.wait(5)
        return real_resolve(name, projects, root, memo)

    monkeypatch.setattr(projectid, "INDEX_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(projectid, "_resolve_folder", slow)
    with mac.on():
        projectid.configure(mac.claude, retained={WIRE: folder})
        idx = projectid.index()
        release.set()
    assert idx.timed_out and idx.owners == {}


def test_recovery_from_a_sidecar_written_in_another_claude_folder_mints_nothing(mac):
    mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    mac.memory(folder, "a.md", "a\n")
    mac.memory("-Users-kb-loose", "b.md", "b\n")
    mac.memory("-opt-shared", "c.md", "c\n")
    mac.push()
    elsewhere = mac.home / "other-claude"
    (elsewhere / "projects/-Users-kb-unrelated/memory").mkdir(parents=True)
    (elsewhere / "projects/-Users-kb-unrelated/memory/z.md").write_text("z\n")
    mac.config["sync"]["sources"][0]["path"] = str(elsewhere)
    mac.claude = elsewhere
    _corrupt_own_manifest(mac)
    mac.push()
    assert mac.published()["tombstones"] == {}


def test_recovery_moves_a_recorded_deletion_to_the_identitys_new_owner(mac):
    import shutil

    checkout = mac.checkout("/Users/kb/old/widgets")
    old = mac.folder_for("/Users/kb/old/widgets")
    mac.memory(old, "keep.md", "k\n")
    doomed = mac.memory(old, "del.md", "d\n")
    mac.push()
    doomed.unlink()
    mac.push()
    new_checkout = mac.fs_root / "Users/kb/new/widgets"
    new_checkout.parent.mkdir(parents=True)
    shutil.move(checkout, new_checkout)
    shutil.move(
        mac.claude / "projects" / old,
        mac.claude / "projects" / mac.folder_for("/Users/kb/new/widgets"),
    )
    _corrupt_own_manifest(mac)
    mac.push()
    assert list(mac.published()["tombstones"]) == [f"claude:projects/{WIRE}/memory/del.md"]


def test_recovery_with_no_resolvable_home_leaves_the_prior_alone(mac, monkeypatch):
    mac.memory("-Users-kb-loose", "a.md", "a\n")
    mac.push()
    _corrupt_own_manifest(mac)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(projectid, "_wire_home", lambda: None)
        mac.push()
    assert mac.first_byte() == crypto.FORMAT_VERSION


def test_moving_the_claude_folder_before_the_first_switch_mints_nothing(mac):
    mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    seed_old_form(mac, {f"{folder}/memory/a.md": "a\n", "-Users-kb-loose/memory/b.md": "b\n"})
    elsewhere = mac.home / "other-claude"
    (elsewhere / "projects/-Users-kb-unrelated/memory").mkdir(parents=True)
    (elsewhere / "projects/-Users-kb-unrelated/memory/z.md").write_text("z\n")
    mac.config["sync"]["sources"][0]["path"] = str(elsewhere)
    mac.claude = elsewhere
    mac.push()
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    assert mac.published()["tombstones"] == {}


def test_a_malformed_deletion_record_never_replaces_a_valid_one():
    good = {"deleted_at": "2026-10-31T00:00:00+00:00"}
    for tombstones in (
        {"claude:projects/-a/memory/x.md": good, "claude:projects/-b/memory/x.md": "junk"},
        {"claude:projects/-a/memory/x.md": "junk", "claude:projects/-b/memory/x.md": good},
    ):
        out = manifest._remap_keys({}, tombstones, lambda folder: "-same")[1]
        assert out == {"claude:projects/-same/memory/x.md": good}


def test_old_form_wording_for_several_files_and_several_peers(tmp_path, store, mac, linux):
    seed_old_form(mac, {"-Users-kb-a/memory/1.md": "1\n", "-Users-kb-a/memory/2.md": "2\n"})
    result = linux.pull()
    assert (
        pullflow.old_form_clause(result)
        == "2 files from mac wait until it publishes shared names (mm status)"
    )
    assert pullflow.old_form_detail(result) == (
        "2 files from mac wait until it publishes shared names"
    )
    assert any(
        line.startswith("2 files from mac wait until it publishes shared names. On mac (")
        for line in linux.cli("status").output.splitlines()
    )
    other = Machine(tmp_path, store, "other", "/Users/other", "dddddddd")
    seed_old_form(other, {"-Users-other-b/memory/3.md": "3\n"})
    both = pullflow.old_form_detail(linux.pull())
    assert both.endswith("wait until they publish shared names")
    assert "2 files from mac" in both and "1 file from other" in both


def test_autopush_held_files_are_never_recorded_as_a_clean_switch(mac, monkeypatch):
    from mind_meld import cli as cli_module

    def fake_push(*args, **kwargs):
        return PushResult(
            wire_names_published=True, held=[("claude", "projects/x/memory/a.md", "changed")]
        )

    monkeypatch.setattr(cli_module, "_push_core", fake_push)
    mac.memory("-Users-kb-loose", "a.md", "a\n")
    assert mac.cli("autopush").exit_code == 0
    crumb = json.loads((mac.sidecar_dir / "last-autorun.json").read_text())["push"]
    assert crumb["outcome"] == "degraded"
    assert "published under shared names" in crumb["detail"]
    assert "projects/x/memory/a.md" in crumb["detail"]


def test_a_carried_wire_name_beside_its_folder_does_not_collide(mac):
    mac.memory("-Users-kb-foo", "a.md", "a\n")
    data = local_manifest(
        {"projects/-Users-kb-foo/memory/a.md": "1"},
        {"claude:projects/@home-foo/memory/gone.md": {"deleted_at": "2026-10-31T00:00:00+00:00"}},
    )
    with mac.on():
        wire = manifest.manifest_to_wire(data)
    assert list(wire["sources"]["claude"]["files"]) == ["projects/@home-foo/memory/a.md"]
    assert list(wire["tombstones"]) == ["claude:projects/@home-foo/memory/gone.md"]


def test_a_symlinked_claude_folder_settles_and_mints_real_deletions(mac):
    import shutil

    real = mac.home / "dotfiles-claude"
    shutil.move(mac.claude, real)
    mac.claude.symlink_to(real)
    mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    mac.memory(folder, "keep.md", "k\n")
    gone = mac.memory(folder, "gone.md", "g\n")
    mac.memory("-Users-kb-loose", "keep.md", "k\n")
    loose = mac.memory("-Users-kb-loose", "gone.md", "g\n")
    assert mac.push() is not None
    assert mac.push() is None  # settles: its own base_path is not "another Claude folder"
    gone.unlink()
    loose.unlink()
    mac.push()
    assert set(mac.published()["tombstones"]) == {
        f"claude:projects/{WIRE}/memory/gone.md",
        "claude:projects/@home-loose/memory/gone.md",
    }


def test_moving_the_claude_folder_mints_nothing_for_any_form(mac):
    """Retargeting `~/.claude` at an empty folder is a move, not a deletion of everything."""
    mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    mac.memory(folder, "a.md", "a\n")
    mac.memory("-Users-kb-loose", "b.md", "b\n")
    mac.memory("-opt-shared", "d.md", "d\n")
    mac.push()
    elsewhere = mac.home / "other-claude"
    (elsewhere / "projects/-Users-kb-unrelated/memory").mkdir(parents=True)
    (elsewhere / "projects/-Users-kb-unrelated/memory/c.md").write_text("c\n")
    mac.config["sync"]["sources"][0]["path"] = str(elsewhere)
    mac.claude = elsewhere
    mac.push()
    assert mac.published()["tombstones"] == {}
    assert mac.push() is None


def test_a_missing_projects_folder_mints_nothing(mac, linux):
    import shutil

    checkout = mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    first = mac.memory(folder, "a.md", "a\n")
    mac.memory("-Users-kb-loose", "b.md", "b\n")
    mac.memory("-opt-shared", "c.md", "c\n")
    mac.push()
    linux.cwd = linux.checkout("/home/kb/x/p")
    linux.pull()
    linux.push()
    shutil.rmtree(mac.claude / "projects")
    mac.push()
    assert mac.published()["tombstones"] == {}
    mac.cwd = checkout
    mac.pull()
    assert first.read_text() == "a\n"  # a pull in the checkout brings the memory back


def test_tombstones_survive_a_home_that_stops_resolving_and_returns(mac, linux):
    mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    mac.memory(folder, "keep.md", "k\n")
    doomed = mac.memory(folder, "del.md", "d\n")
    mac.push()
    linux.cwd = linux.checkout("/home/kb/x/p")
    linux.pull()
    linux.push()
    doomed.unlink()
    mac.push()
    key = f"claude:projects/{WIRE}/memory/del.md"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(projectid, "_wire_home", lambda: None)
        mac.memory(folder, "more.md", "m\n")
        mac.push()
        assert mac.first_byte() == crypto.FORMAT_VERSION
        assert list(mac.published()["tombstones"]) == [key]
    mac.pull()
    assert not doomed.exists()
    mac.push()
    assert mac.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    assert list(mac.published()["tombstones"]) == [key]


def test_a_carried_tombstone_over_name_max_does_not_stop_pushes(mac):
    import shutil

    path = "acme/" + "x" * 280
    checkout = mac.checkout("/Users/kb/proj", f"git@github.com:{path}.git")
    folder = mac.folder_for("/Users/kb/proj")
    mac.memory(folder, "keep.md", "k\n")
    doomed = mac.memory(folder, "del.md", "d\n")
    mac.push()
    doomed.unlink()
    mac.push()
    shutil.rmtree(checkout)
    mac.memory(folder, "more.md", "m\n")
    assert mac.push() is not None  # no ENAMETOOLONG from probing a wire name as a path
    assert len(mac.published()["tombstones"]) == 1


def test_sidecar_recovery_respells_a_carried_tombstone(mac):
    """The checkout was away at the last push, and is back when recovery runs."""
    import shutil

    checkout = mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    mac.memory(folder, "keep.md", "k\n")
    doomed = mac.memory(folder, "del.md", "d\n")
    mac.push()
    doomed.unlink()
    mac.push()
    away = checkout.with_name("away")
    shutil.move(checkout, away)
    mac.memory(folder, "more.md", "m\n")
    mac.push()
    shutil.move(away, checkout)
    doomed.write_text("recreated\n")
    _corrupt_own_manifest(mac)
    mac.push()
    # The file is back: its deletion record clears instead of sitting on the same key.
    assert mac.published()["tombstones"] == {}
    assert f"projects/{WIRE}/memory/del.md" in wire_keys(mac.published())


def test_sidecar_recovery_with_the_checkout_away_keeps_the_deletion(mac, linux):
    import shutil

    checkout = mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    mac.memory(folder, "keep.md", "k\n")
    doomed = mac.memory(folder, "del.md", "d\n")
    mac.push()
    linux.cwd = linux.checkout("/home/kb/x/p")
    linux.pull()
    linux.push()
    doomed.unlink()
    mac.push()
    away = checkout.with_name("away")
    shutil.move(checkout, away)
    _corrupt_own_manifest(mac)
    mac.push()
    assert list(mac.published()["tombstones"]) == [f"claude:projects/{WIRE}/memory/del.md"]
    shutil.move(away, checkout)
    mac.pull()
    assert not doomed.exists()


@pytest.mark.parametrize("parent_format", [0, 99])
def test_git_fallback_never_reads_a_parent_repository(mac, parent_format):
    import subprocess

    parent = mac.fs_root / "Users/kb/parent"
    parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    with (parent / ".git/config").open("a") as fh:
        fh.write('[remote "origin"]\n\turl = git@github.com:acme/parent.git\n')
        # A format git refuses is what used to send a relative --file up a level.
        fh.write(f"[core]\n\trepositoryformatversion = {parent_format}\n")
    child = mac.checkout("/Users/kb/parent/child", None)
    with (child / ".git/config").open("a") as fh:
        fh.write(f'[remote "origin"]\n\turl = "{GIT_URL}"\n')
    folder = mac.folder_for("/Users/kb/parent/child")
    mac.memory(folder, text="x\n")
    assert index_for(mac).owners == {IDENTITY: folder}  # its own file, never the parent's


def test_a_marked_sidecar_builds_the_index_once_with_its_retained_identity(mac, monkeypatch):
    mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    mac.memory(folder, "a.md", "a\n")
    mac.push()
    assert sidecar.read.__module__ == "mind_meld.sidecar"
    builds = []
    real_build = projectid._build_index

    def counted(*args, **kwargs):
        builds.append(args[1])
        return real_build(*args, **kwargs)

    monkeypatch.setattr(projectid, "_build_index", counted)
    with mac.on():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sidecar, "SIDECAR_DIR", mac.sidecar_dir)
            idx = projectid.index()
    assert builds == [{folder: IDENTITY}]  # one build, with the retained identity
    assert idx.owners == {IDENTITY: folder}


def test_a_deleted_folder_with_an_unreadable_checkout_mints_nothing(mac, linux):
    """Unreadable may hide a moved checkout, and a false tombstone blocks the fleet."""
    import shutil

    mac.checkout("/Users/kb/proj")
    folder = mac.folder_for("/Users/kb/proj")
    mac.memory(folder, "a.md", "a\n")
    mac.memory("-Users-kb-loose", "other.md", "o\n")
    mac.push()
    shutil.rmtree(mac.claude / "projects" / folder)
    real_lstat = projectid._lstat

    def denied(path):
        if path.name == ".git":
            raise projectid._Unknown(projectid.WHY_UNREADABLE)
        return real_lstat(path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(projectid, "_lstat", denied)
        pushed = mac.push()
    assert pushed.total_deleted == 0
    assert mac.published()["tombstones"] == {}


def test_git_fallback_follows_an_include_with_real_git(mac):
    path = mac.checkout("/Users/kb/Dev/widgets", None)
    with (path / ".git/config").open("a") as fh:
        fh.write("[include]\n\tpath = origin.inc\n")
    (path / ".git/origin.inc").write_text(f'[remote "origin"]\n\turl = {GIT_URL}\n')
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    mac.memory(folder, text="x\n")
    assert index_for(mac).owners == {IDENTITY: folder}


# ── the cross-vendor pass ────────────────────────────────────────────


def _republish_without(machine: Machine, wire_folder: str) -> bytes:
    """Rewrite a machine's canonical manifest without one project; return the old bytes."""
    path = Path(machine.store) / manifest_key(machine.device_id)
    raw = path.read_bytes()
    with machine.on():
        data = json.loads(crypto.decrypt_manifest(raw, PASSPHRASE, MEMORY_KB))
        files = data["sources"]["claude"]["files"]
        data["sources"]["claude"]["files"] = {
            k: v for k, v in files.items() if not k.startswith(f"projects/{wire_folder}/")
        }
        path.write_bytes(
            crypto.encrypt_manifest(
                json.dumps(data).encode(), PASSPHRASE, MEMORY_KB, wire_names=True
            )
        )
    return raw


@pytest.mark.parametrize("source_filter", [None, "claude"])
def test_a_pull_keeps_the_conflict_copy_that_alone_names_unplaced_memory(mac, linux, source_filter):
    """The copy may be the only manifest naming that memory, and `mm gc` reads manifests."""
    mac.checkout("/Users/kb/proj")
    mac.memory(mac.folder_for("/Users/kb/proj"), "p.md", "only here\n")
    mac.memory("-Users-kb-loose", "t.md", "applies\n")
    mac.push()
    canonical = Path(mac.store) / manifest_key(mac.device_id)
    older = _republish_without(mac, WIRE)
    copy = canonical.with_name(canonical.name.replace(".enc", " 2.enc"))
    copy.write_bytes(older)

    (linux.claude / "projects" / "-home-kb-other").mkdir(parents=True)
    result = linux.pull(source_filter=source_filter)
    assert result.total_applied == 1  # the loose file arrived, so this peer "had changes"
    assert (linux.claude / "projects/-home-kb-loose/memory/t.md").exists()
    assert copy.exists()
    assert result.not_placed_projects == 1


def test_a_pull_of_another_source_keeps_that_conflict_copy_too(mac, linux):
    notes = mac.home / "notes-src"
    (notes / "d").mkdir(parents=True)
    (notes / "d/n.md").write_text("n\n")
    mac.config["sync"]["sources"].append(
        {"name": "notes", "path": str(notes), "type": "generic", "include_dirs": ["d"]}
    )
    mac.checkout("/Users/kb/proj")
    mac.memory(mac.folder_for("/Users/kb/proj"), "p.md", "only here\n")
    mac.push()
    canonical = Path(mac.store) / manifest_key(mac.device_id)
    older = _republish_without(mac, WIRE)
    copy = canonical.with_name(canonical.name.replace(".enc", " 2.enc"))
    copy.write_bytes(older)
    other_notes = linux.home / "notes-src"
    other_notes.mkdir()
    linux.config["sync"]["sources"].append(
        {"name": "notes", "path": str(other_notes), "type": "generic", "include_dirs": ["d"]}
    )
    linux.pull(source_filter="notes")
    assert (other_notes / "d/n.md").exists()  # this peer "had changes"
    assert copy.exists()


def test_a_pull_still_cleans_a_conflict_copy_when_nothing_is_withheld(mac, linux):
    mac.memory("-Users-kb-loose", "t.md", "applies\n")
    mac.push()
    canonical = Path(mac.store) / manifest_key(mac.device_id)
    copy = canonical.with_name(canonical.name.replace(".enc", " 2.enc"))
    copy.write_bytes(canonical.read_bytes())
    linux.pull()
    assert (linux.claude / "projects/-home-kb-loose/memory/t.md").exists()
    assert not copy.exists()


def _claim_this_claude_folder(machine: Machine) -> None:
    """The store's old-form manifest was written from this very Claude folder."""
    path = Path(machine.store) / manifest_key(machine.device_id)
    with machine.on():
        data = json.loads(crypto.decrypt_manifest(path.read_bytes(), PASSPHRASE, MEMORY_KB))
        data["sources"]["claude"]["base_path"] = os.path.realpath(machine.claude)
        path.write_bytes(
            crypto.encrypt_manifest(
                json.dumps(data).encode(), PASSPHRASE, MEMORY_KB, wire_names=False
            )
        )


def _old_form_with_removed_folder(linux: Machine) -> tuple[Path, str]:
    """A machine on the old form whose project folder is gone; its checkout is not."""
    import shutil

    checkout = linux.checkout("/home/kb/proj")
    folder = linux.folder_for("/home/kb/proj")
    seed_old_form(linux, {f"{folder}/memory/a.md": "a\n", "-home-kb-other/memory/o.md": "o\n"})
    _claim_this_claude_folder(linux)
    shutil.rmtree(linux.claude / "projects" / folder)
    return checkout, folder


def test_a_deletion_minted_by_the_first_switch_stays_home_relative(mac, linux):
    """A folder gone since the last old-form push may be housekeeping, and an identity-wide
    record would block that project fleet-wide. It keeps the path-scoped spelling."""
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "a\n")
    mac.push()
    _, folder = _old_form_with_removed_folder(linux)
    linux.push()
    assert list(linux.published()["tombstones"]) == ["claude:projects/@home-proj/memory/a.md"]
    assert linux.pull().total_applied == 0  # elsewhere, the project stays unplaced
    assert not (linux.claude / "projects" / folder).exists()


def test_a_removed_clone_never_outranks_the_live_checkout(mac, linux):
    import shutil

    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "a\n")
    mac.push()
    linux.checkout("/home/kb/archive/proj")
    live = linux.checkout("/home/kb/code/proj")
    archive_folder = linux.folder_for("/home/kb/archive/proj")
    seed_old_form(
        linux, {f"{archive_folder}/memory/old.md": "old\n", "-home-kb-other/memory/o.md": "o\n"}
    )
    shutil.rmtree(linux.claude / "projects" / archive_folder)
    seed_old_form(linux, {})
    linux.push()
    linux.cwd = live
    linux.pull()
    assert (
        linux.claude / "projects" / linux.folder_for("/home/kb/code/proj") / "memory/a.md"
    ).exists()
    assert not (linux.claude / "projects" / archive_folder).exists()


def test_conflict_copy_retention_is_per_device(tmp_path, store, mac, linux):
    other = Machine(tmp_path, store, "other", "/Users/other", "dddddddd")
    mac.checkout("/Users/kb/proj")
    mac.memory(mac.folder_for("/Users/kb/proj"), "p.md", "p\n")
    mac.memory("-Users-kb-loose", "t.md", "t\n")
    mac.push()
    other.memory("-Users-other-side", "s.md", "s\n")
    other.push()
    copies = []
    for machine in (mac, other):
        canonical = Path(store) / manifest_key(machine.device_id)
        copy = canonical.with_name(canonical.name.replace(".enc", " 2.enc"))
        copy.write_bytes(canonical.read_bytes())
        copies.append(copy)
    (linux.claude / "projects" / "-home-kb-mine").mkdir(parents=True)
    linux.pull()
    assert copies[0].exists()  # the mac's project is withheld here
    assert not copies[1].exists()  # nothing of the other machine's is


def test_a_removed_folders_record_holds_against_a_same_home_peer_elsewhere(tmp_path, store, linux):
    twin = Machine(tmp_path, store, "twin", "/home/kb", "dddddddd")
    _, folder = _old_form_with_removed_folder(linux)
    seed_old_form(linux, {})
    seed_old_form(twin, {f"{folder}/memory/a.md": "a\n"})  # the same path, still on old names
    linux.push()
    assert linux.cli("autopull").exit_code == 0  # from the home folder, with no owner
    assert not (linux.claude / "projects" / folder / "memory/a.md").exists()
    assert set(linux.published()["tombstones"]) == {"claude:projects/@home-proj/memory/a.md"}


def test_a_push_that_cannot_list_projects_holds_the_first_switch(mac, linux):
    """Unlisted, nothing would own a project, so its records would take home names its folder
    ignores once it returns. The next listed push switches with those records still held."""
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "x.md", "peer\n")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "y.md", "other\n")
    mac.push()
    linux.checkout("/home/kb/alive/proj")
    linux.checkout("/home/kb/zclone/proj")
    owner = linux.folder_for("/home/kb/alive/proj")
    clone = linux.folder_for("/home/kb/zclone/proj")
    owner_gone = linux.memory(owner, "x.md", "x\n")
    clone_gone = linux.memory(clone, "a.md", "a\n")
    seed_old_form(linux, {f"{owner}/memory/keep.md": "k\n", f"{clone}/memory/c.md": "c\n"})
    owner_gone.unlink()
    clone_gone.unlink()
    seed_old_form(linux, {})  # the sidecar now holds both records
    _claim_this_claude_folder(linux)
    projects = linux.claude / "projects"
    parked = linux.claude / "projects.parked"
    projects.rename(parked)
    linux.push()
    assert linux.first_byte() == crypto.FORMAT_VERSION
    held = {f"claude:projects/{owner}/memory/x.md", f"claude:projects/{clone}/memory/a.md"}
    assert set(linux.published()["tombstones"]) == held
    parked.rename(projects)

    linux.push()
    assert linux.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    assert set(linux.published()["tombstones"]) == {
        f"claude:projects/{WIRE}/memory/x.md",
        "claude:projects/@home-zclone-proj/memory/a.md",
    }
    linux.pull()
    landed = linux.claude / "projects" / owner / "memory"
    assert not (landed / "x.md").exists()  # stays deleted
    assert (landed / "y.md").read_text() == "other\n"  # while the project still syncs


def test_a_worktree_folder_owning_a_removed_folders_project_makes_its_records_identity_wide(linux):
    """The limit `docs/invariants/sync.md` states: the nominated folder owns the identity."""
    import shutil

    main = linux.checkout("/home/kb/proj")
    folder = linux.folder_for("/home/kb/proj")
    worktree = _worktree_of(linux, main, "/home/kb/wt/feat")
    seed_old_form(linux, {f"{folder}/memory/a.md": "a\n", f"{worktree}/memory/w.md": "w\n"})
    _claim_this_claude_folder(linux)
    shutil.rmtree(linux.claude / "projects" / folder)
    linux.push()
    assert set(linux.published()["tombstones"]) == {f"claude:projects/{WIRE}/memory/a.md"}


def test_a_missed_sidecar_write_then_an_outage_leaves_a_live_clones_record_on_its_path(mac, linux):
    """The switch's sidecar write fails, then a push runs with `projects/` unlisted. A live
    second clone's deletion binds only that clone: the peer's same-named file still arrives."""
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "peer\n")
    mac.push()
    linux.checkout("/home/kb/alive/proj")
    linux.checkout("/home/kb/zclone/proj")
    owner = linux.folder_for("/home/kb/alive/proj")
    clone = linux.folder_for("/home/kb/zclone/proj")
    gone = linux.memory(clone, "a.md", "a\n")
    seed_old_form(linux, {f"{owner}/memory/keep.md": "k\n", f"{clone}/memory/c.md": "c\n"})
    gone.unlink()
    seed_old_form(linux, {})
    _claim_this_claude_folder(linux)
    with linux.on():
        stale = sidecar.sidecar_path().read_bytes()
    linux.push()  # the switch is accepted
    with linux.on():
        sidecar.sidecar_path().write_bytes(stale)  # and its sidecar write fails
    projects = linux.claude / "projects"
    parked = linux.claude / "projects.parked"
    projects.rename(parked)
    linux.push()
    parked.rename(projects)
    linux.push()
    assert f"claude:projects/{WIRE}/memory/a.md" not in linux.published()["tombstones"]
    linux.pull()
    assert (projects / owner / "memory/a.md").read_text() == "peer\n"


def test_losing_projects_mid_push_never_binds_an_undeleted_file_to_its_project(linux, monkeypatch):
    """`projects/` goes between the manifest load and the scan, so that push cannot see
    what it published. Only one project comes back, and nobody deleted the other's file:
    the push mints no record for any file it could not list (Track 91A), whichever name
    it would have been filed under."""
    from mind_meld import cli as cli_module

    linux.checkout("/home/kb/x/proj")
    linux.checkout("/home/kb/y/other", "git@github.com:acme/other.git")
    x = linux.folder_for("/home/kb/x/proj")
    y = linux.folder_for("/home/kb/y/other")
    seed_old_form(linux, {f"{x}/memory/a.md": "a\n", f"{y}/memory/e.md": "e\n"})
    _claim_this_claude_folder(linux)
    projects = linux.claude / "projects"
    real = cli_module.build_manifest_v2

    def vanish_then_scan(*args, **kwargs):
        if projects.exists():
            projects.rename(linux.claude / "projects.parked")
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(cli_module, "build_manifest_v2", vanish_then_scan)
        linux.push()
    assert linux.published()["tombstones"] == {}  # the losing push minted nothing
    linux.memory(y, "e.md", "e\n")
    linux.push()
    tombstones = linux.published()["tombstones"]
    assert f"claude:projects/{WIRE}/memory/a.md" not in tombstones
    assert "claude:projects/@home-x-proj/memory/a.md" not in tombstones
    assert tombstones == {}  # a recreated `projects/` is new: nothing under it is a deletion


def test_the_switch_decision_and_its_names_come_from_one_listing(linux, monkeypatch):
    """`projects/` goes after the decision, during the upload. The push switches on what it
    listed, so an owner's deletion record still binds the project."""
    from mind_meld import cli as cli_module

    linux.checkout("/home/kb/proj")
    folder = linux.folder_for("/home/kb/proj")
    gone = linux.memory(folder, "x.md", "x\n")
    seed_old_form(linux, {f"{folder}/memory/keep.md": "k\n"})
    gone.unlink()
    seed_old_form(linux, {})
    _claim_this_claude_folder(linux)
    linux.memory(folder, "new.md", "n\n")
    projects = linux.claude / "projects"
    real = cli_module._upload_changed_blobs

    def vanish_after(*args, **kwargs):
        out = real(*args, **kwargs)
        if projects.exists():
            projects.rename(linux.claude / "projects.parked")
        return out

    with monkeypatch.context() as patch:
        patch.setattr(cli_module, "_upload_changed_blobs", vanish_after)
        linux.push()
    assert linux.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    assert set(linux.published()["tombstones"]) == {f"claude:projects/{WIRE}/memory/x.md"}


def test_a_held_switch_is_announced_by_a_preview_beside_a_pending_change(linux):
    """A preview says why the names are not published. Nothing under the unlistable
    `projects/` counts as a deletion, so it takes another pending change to publish
    anything (Track 91A: a deletion no longer makes the held push substantive)."""
    notes = _with_notes_source(linux)
    _held_machine(linux)
    (notes / "d/n.md").write_text("changed\n")
    before = LocalBackend(linux.store).get(manifest_key(linux.device_id))
    preview = linux.cli("push", "--dry-run")
    assert preview.exit_code == 0, preview.output
    assert "shared project names" not in preview.output
    assert "cannot be listed" in preview.output
    assert LocalBackend(linux.store).get(manifest_key(linux.device_id)) == before


def test_a_held_switch_says_project_memory_is_not_published(linux):
    from mind_meld.cli import _sync_success

    linux.checkout("/home/kb/proj")
    folder = linux.folder_for("/home/kb/proj")
    seed_old_form(linux, {f"{folder}/memory/a.md": "a\n"})
    _claim_this_claude_folder(linux)
    (linux.claude / "projects").rename(linux.claude / "projects.parked")
    preview = linux.cli("push", "--dry-run")
    assert preview.exit_code == 0, preview.output
    assert "shared project names" not in preview.output
    # Nothing is pending: the files the held push cannot list are left out, not deleted.
    assert "Nothing to push" in preview.output
    with linux.on():
        result = _push_core(
            linux.config,
            PASSPHRASE,
            MEMORY_KB,
            quiet=True,
            attended=True,
            result_observer=lambda r: None,
        )
    assert linux.first_byte() == crypto.FORMAT_VERSION
    # The parked folder's file is truthfully reported as left out beside the held switch.
    line = _sync_success("push", result)
    assert line.startswith(
        "Push: project memory not published until Claude's projects folder can be listed; "
    )
    assert "1 file left out because" in line and line.endswith("run mm pull to restore them.")
    assert _sync_success("push", PushResult(published_content_files=1, switch_held=True)) == (
        "Push: 1 content change published; "
        "project memory not published until Claude's projects folder can be listed."
    )


def test_a_file_deleted_in_a_folder_gone_at_the_switch_can_return_inside_its_checkout(mac, linux):
    """Karl's accepted limit: nothing ties a removed folder's record to its project, so it
    binds that path only until a pull inside the checkout owns the identity."""
    import shutil

    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "a\n")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "b.md", "b\n")
    mac.push()

    checkout = linux.checkout("/home/kb/proj")
    folder = linux.folder_for("/home/kb/proj")
    seed_old_form(linux, {f"{folder}/memory/a.md": "a\n", "-home-kb-other/memory/o.md": "o\n"})
    shutil.rmtree(linux.claude / "projects" / folder)
    seed_old_form(linux, {})
    linux.push()  # the first switch
    assert linux.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    assert set(linux.published()["tombstones"]) == {"claude:projects/@home-proj/memory/a.md"}

    assert linux.cli("autopull").exit_code == 0  # from elsewhere the project stays unplaced
    assert not (linux.claude / "projects" / folder).exists()
    linux.cwd = checkout
    linux.pull()
    landed = linux.claude / "projects" / folder / "memory"
    assert (landed / "a.md").read_text() == "a\n"
    assert (landed / "b.md").read_text() == "b\n"


def test_a_live_folder_with_its_checkout_away_at_the_switch_keeps_home_names(mac, linux):
    """The same limit for a live folder: once the checkout returns and the folder owns the
    project, the home-relative record no longer binds it and any pull restores the file."""
    mac.checkout("/Users/kb/Dev/widgets")
    mac.memory(mac.folder_for("/Users/kb/Dev/widgets"), "a.md", "peer\n")
    mac.push()
    checkout = linux.checkout("/home/kb/proj")
    folder = linux.folder_for("/home/kb/proj")
    gone = linux.memory(folder, "a.md", "a\n")
    seed_old_form(linux, {f"{folder}/memory/keep.md": "k\n"})
    gone.unlink()
    seed_old_form(linux, {})
    _claim_this_claude_folder(linux)
    away = checkout.with_name("proj.away")
    checkout.rename(away)
    linux.push()
    away.rename(checkout)
    assert set(linux.published()["tombstones"]) == {"claude:projects/@home-proj/memory/a.md"}
    linux.pull()
    assert (linux.claude / "projects" / folder / "memory/a.md").read_text() == "peer\n"


def test_a_removed_folders_record_keeps_its_home_name_through_a_held_switch(linux):
    _, folder = _old_form_with_removed_folder(linux)
    seed_old_form(linux, {})  # the sidecar now holds the record
    _claim_this_claude_folder(linux)
    projects = linux.claude / "projects"
    parked = linux.claude / "projects.parked"
    projects.rename(parked)
    linux.push()
    assert linux.first_byte() == crypto.FORMAT_VERSION
    assert set(linux.published()["tombstones"]) == {f"claude:projects/{folder}/memory/a.md"}
    parked.rename(projects)
    linux.push()
    assert linux.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    assert set(linux.published()["tombstones"]) == {"claude:projects/@home-proj/memory/a.md"}


@pytest.mark.parametrize(
    "prior, listed, due",
    [
        (None, True, (True, True)),
        ({"sources": {"claude": {}}}, True, (True, True)),
        ({"sources": {"claude": {"wire_names": True}}}, True, (True, False)),
        (None, False, (False, False)),
        ({"sources": {"claude": {}}}, False, (False, False)),
        ({"sources": {"claude": {"wire_names": True}}}, False, (True, False)),
    ],
)
def test_only_the_first_switch_needs_a_listing(mac, prior, listed, due):
    (mac.claude / "projects").mkdir(parents=True, exist_ok=True)
    if not listed:
        (mac.claude / "projects").rmdir()
    with mac.on():
        form = core.project_form_state({"sources": {"claude": {"files": {}}}}, prior)
    assert (form.publish_new, form.switch_due) == due
    assert form.switch_held == (not listed and due == (False, False))


def _with_notes_source(machine: Machine) -> Path:
    """A second source, so a push has a content change while `projects/` is gone."""
    notes = machine.home / "notes-src"
    (notes / "d").mkdir(parents=True)
    (notes / "d/n.md").write_text("n\n")
    machine.config["sync"]["sources"].append(
        {"name": "notes", "path": str(notes), "type": "generic", "include_dirs": ["d"]}
    )
    return notes


def test_a_held_push_collects_no_blobs(linux):
    notes = _with_notes_source(linux)
    linux.checkout("/home/kb/proj")
    folder = linux.folder_for("/home/kb/proj")
    seed_old_form(linux, {f"{folder}/memory/a.md": "only copy\n"})
    _claim_this_claude_folder(linux)
    backend = LocalBackend(linux.store)
    blobs = set(backend.list_keys("data/"))
    (linux.claude / "projects").rename(linux.claude / "projects.parked")
    (notes / "d/n.md").write_text("changed\n")
    pushed = linux.cli("push")
    assert pushed.exit_code == 0, pushed.output
    assert linux.first_byte() == crypto.FORMAT_VERSION
    assert blobs <= set(backend.list_keys("data/"))


def test_a_switched_machine_that_cannot_list_projects_collects_no_blobs(linux):
    notes = _with_notes_source(linux)
    linux.checkout("/home/kb/proj")
    linux.memory(linux.folder_for("/home/kb/proj"), "a.md", "only copy\n")
    linux.push()
    assert linux.first_byte() == crypto.MANIFEST_FORMAT_VERSION
    backend = LocalBackend(linux.store)
    blobs = set(backend.list_keys("data/"))
    (linux.claude / "projects").rename(linux.claude / "projects.parked")
    (notes / "d/n.md").write_text("changed\n")
    pushed = linux.cli("push")
    assert pushed.exit_code == 0, pushed.output
    assert blobs <= set(backend.list_keys("data/"))


def test_losing_projects_mid_push_collects_no_blobs(linux, monkeypatch):
    """A switched machine's index was built at the load, while `projects/` was still there."""
    from mind_meld import cli as cli_module

    linux.checkout("/home/kb/proj")
    linux.memory(linux.folder_for("/home/kb/proj"), "a.md", "only copy\n")
    linux.push()
    backend = LocalBackend(linux.store)
    blobs = set(backend.list_keys("data/"))
    projects = linux.claude / "projects"
    real = cli_module.build_manifest_v2

    def vanish_then_scan(*args, **kwargs):
        if projects.exists():
            projects.rename(linux.claude / "projects.parked")
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(cli_module, "build_manifest_v2", vanish_then_scan)
        pushed = linux.cli("push")
    assert pushed.exit_code == 0, pushed.output
    assert blobs <= set(backend.list_keys("data/"))


def _held_machine(linux: Machine) -> None:
    linux.checkout("/home/kb/proj")
    folder = linux.folder_for("/home/kb/proj")
    seed_old_form(linux, {f"{folder}/memory/a.md": "a\n"})
    _claim_this_claude_folder(linux)
    (linux.claude / "projects").rename(linux.claude / "projects.parked")


def test_status_during_a_hold_says_project_memory_is_not_published(linux):
    _held_machine(linux)
    status = linux.cli("status")
    assert status.exit_code == 0, status.output
    assert "ready to publish" not in status.output
    assert "Local content matches" not in status.output
    held = "not published until Claude's projects folder can be listed"
    assert held in status.output.splitlines()[0]  # the verdict
    assert status.output.count(held) == 2  # and the status body


def test_status_names_a_hold_beside_other_pending_changes(linux):
    notes = _with_notes_source(linux)
    _held_machine(linux)
    (notes / "d/n.md").write_text("changed\n")
    status = linux.cli("status")
    assert status.exit_code == 0, status.output
    assert "Overall pending push" in status.output
    assert "not published until Claude's projects folder can be listed" in status.output


def test_a_second_held_push_still_says_project_memory_is_not_published(linux):
    _held_machine(linux)
    first = linux.cli("push")
    assert first.exit_code == 0, first.output
    assert "not published until Claude's projects folder can be listed" in first.output
    # The held push publishes nothing: the files it cannot list stay as they were, with no
    # record of a deletion (it used to drop them and mint one).
    published = linux.published()
    assert list(published["sources"]["claude"]["files"]) == ["projects/-home-kb-proj/memory/a.md"]
    assert published["tombstones"] == {}
    second = linux.cli("push")
    assert second.exit_code == 0, second.output
    assert "not published until Claude's projects folder can be listed" in second.output


def test_a_held_no_op_push_records_no_changes_in_its_run_evidence(linux, monkeypatch):
    from mind_meld import attemptlog

    # A skipped usage capture would read as degraded and hide the cause under test.
    monkeypatch.setattr(config_module, "usage_capture_readiness", lambda *a, **k: "no-reader")
    notes = _with_notes_source(linux)
    _held_machine(linux)
    (notes / "d/n.md").write_text("changed\n")  # a held push publishes only if something changed
    assert linux.cli("push").exit_code == 0
    with linux.on():
        assert attemptlog.read_runs()["runs"]["push"]["cause"] == "ok"  # it published a manifest
    assert linux.cli("push").exit_code == 0
    with linux.on():
        assert attemptlog.read_runs()["runs"]["push"]["cause"] == "no-changes"


def test_diag_names_a_hold(linux):
    _held_machine(linux)
    diag = linux.cli("diag", "--json")
    assert diag.exit_code == 0, diag.output
    state = json.loads(diag.output)["project_index"]
    assert state["state"] == "path"
    assert state["reason"] == "projects_unlisted"


# ── name-only diff adjustment (audit-added) ──────────────────────────


def _claude_src(files: dict[str, str], **extra) -> dict:
    return {"files": {rel: {"sha256": sha} for rel, sha in files.items()}, **extra}


# Value: protects=a rename is not counted as deletions, a real deletion beside it still counts
# Value: fails_when=the adjuster drops all @ deletions or shows a rename as new+deleted
# Value: why_new=no test calls core.content_counts/names_only_change on bare manifests; e2e only
# Value: seam=none; no_claim=full pull apply (pure functions on hand-built manifests)
def test_name_only_adjustment_hides_a_rename_but_keeps_a_real_deletion():
    old = "projects/@git-github.com%2Facme%2Fwidgets/memory/a.md"
    new = "projects/-Users-kb-widgets/memory/a.md"
    gone = "projects/-Users-kb-other/memory/b.md"
    local = {"sources": {"claude": _claude_src({new: "s1"})}}
    renamed_only = {"claude": _claude_src({old: "s1"})}
    assert core.content_counts(local, renamed_only) == (0, 0, 0)
    assert core.names_only_change(local, renamed_only) is True
    mixed = {"claude": _claude_src({old: "s1", gone: "s2"})}
    assert core.content_counts(local, mixed) == (0, 0, 1)
    assert core.names_only_change(local, mixed) is False


# Value: protects=a freed deletion pairs with one new file only, a duplicate still shows as new
# Value: fails_when=freed counts are not decremented, hiding a genuinely new file with the same sha
# Value: why_new=the pairing counter is exercised by no existing test; seam=none
# Value: no_claim=none beyond protects
def test_name_only_adjustment_pairs_each_freed_deletion_once():
    old = "projects/@git-github.com%2Facme%2Fwidgets/memory/a.md"
    a = "projects/-Users-kb-widgets/memory/a.md"
    b = "projects/-Users-kb-widgets2/memory/a.md"
    local = {"sources": {"claude": _claude_src({a: "s1", b: "s1"})}}
    remote = {"claude": _claude_src({old: "s1"})}
    assert core.content_counts(local, remote) == (1, 0, 0)
    assert core.names_only_change(local, remote) is False


# Value: protects=a moved folder frees its deletions; a placed folder does not
# Value: fails_when=moved_folders is ignored (false deletion count) or applied to every folder
# Value: why_new=moved_folders handling in the diff has no direct test; seam=none
# Value: no_claim=none beyond protects
def test_moved_folders_free_only_their_own_deletions():
    moved = "projects/-Users-kb-old/memory/a.md"
    kept = "projects/-Users-kb-live/memory/c.md"
    local = {"sources": {"claude": _claude_src({})}}
    remote = {"claude": _claude_src({moved: "s1", kept: "s3"}, moved_folders=["-Users-kb-old"])}
    assert core.content_counts(local, remote) == (0, 0, 1)


# Value: protects=only git and home wire names keep a machine's deletion records; old/lit never do
# Value: fails_when=carries_a_deletion widens to @old-/@lit- or loses a home form
# Value: why_new=carries_a_deletion and _split_project_key have no direct test; seam=none
# Value: no_claim=none beyond protects
def test_deletion_carrying_names_and_project_key_split():
    assert projectid.carries_a_deletion(WIRE)
    assert projectid.carries_a_deletion("@home")
    assert projectid.carries_a_deletion("@home-wt-feature")
    assert not projectid.carries_a_deletion("@old-Users-kb-x")
    assert not projectid.carries_a_deletion("@lit-x")
    assert not projectid.carries_a_deletion("-Users-kb-x")
    assert manifest._split_project_key("projects/f/a/b.md") == ("f", "a/b.md")
    for bad in ("projects/f", "projects//a", "projects/f/", "other/f/a", "projects"):
        assert manifest._split_project_key(bad) is None


# Value: protects=reconcile_recovered_prior leaves a prior with no claude entry untouched
# Value: fails_when=it raises on or rewrites a sidecar prior it cannot reason about
# Value: why_new=the early-return guards of reconcile_recovered_prior are untested; seam=none
# Value: no_claim=moved-checkout rewriting (needs a live index)
def test_reconcile_recovered_prior_passes_through_without_a_claude_entry():
    assert core.reconcile_recovered_prior(None) is None
    prior = {"sources": {"gstack": {"files": {}}}, "tombstones": {"x": 1}}
    snapshot = json.dumps(prior, sort_keys=True)
    assert core.reconcile_recovered_prior(prior) is prior
    assert json.dumps(prior, sort_keys=True) == snapshot


# ── Track 91A: fast-forward and propagated deletions across a fleet ───
#
# A Mac and two Linux machines that share a home (`/home/kb`). Both delivery forms
# run: "git" is one project every machine has checked out (shared `@git-` name, three
# different folder names), "home" is a loose folder under the home (the `@home-` name
# maps the Mac's `-Users-kb-scratch` onto both Linux machines' identical
# `-home-kb-scratch`, which is also the old same-home path form).


@pytest.fixture
def linux2(tmp_path, store):
    return Machine(tmp_path, store, "linux2", "/home/kb", "cccccccc")


CHECKOUTS = {
    "mac": "/Users/kb/Dev/widgets",
    "linux": "/home/kb/code/widgets",
    "linux2": "/home/kb/src/widgets",
}
KINDS = pytest.mark.parametrize("kind", ["git", "home"])


class Trio:
    """The same project's memory on a Mac and two Linux machines, with a logical clock."""

    def __init__(self, kind: str, mac: Machine, linux: Machine, linux2: Machine):
        self.kind = kind
        self.mac, self.linux, self.linux2 = mac, linux, linux2
        self.machines = {m.name: m for m in (mac, linux, linux2)}
        self.clock = time.time() - 7200
        self.dirs = {m.name: self._place(m) for m in self.machines.values()}

    def _place(self, machine: Machine) -> Path:
        if self.kind == "git":
            rel = CHECKOUTS[machine.name]
            machine.checkout(rel)
            folder = machine.folder_for(rel)
        else:
            folder = machine.folder_for(f"{machine.home_path}/scratch")
        memory = machine.claude / "projects" / folder / "memory"
        memory.mkdir(parents=True)
        return memory

    def tick(self) -> float:
        self.clock += 60
        return self.clock

    def path(self, machine: Machine, name: str = "x.md") -> Path:
        return self.dirs[machine.name] / name

    def write(self, machine: Machine, name: str, text: str, *, mtime: float | None = None) -> Path:
        """Write a file with the next logical mtime (or an explicit one)."""
        target = self.path(machine, name)
        target.write_text(text)
        stamp = self.tick() if mtime is None else mtime
        os.utime(target, (stamp, stamp))
        return target

    def text(self, machine: Machine, name: str = "x.md") -> str | None:
        target = self.path(machine, name)
        return target.read_text() if target.exists() else None


@pytest.fixture(params=["git", "home"])
def trio(request, mac, linux, linux2):
    return Trio(request.param, mac, linux, linux2)


def deleted_copies(machine: Machine) -> list[Path]:
    """Every recovery copy this machine holds, whichever pull made it."""
    return sorted(p for p in (machine.state_dir / "deleted").rglob("*") if p.is_file())


def ledger_of(machine: Machine, *peers: Machine):
    with machine.on():
        ledger, exc = core.load_ledger([p.device_id for p in peers])
    assert exc is None, exc
    return ledger


def tombstone_for(published: dict, name: str = "x.md") -> dict | None:
    for key, record in published["tombstones"].items():
        if key.endswith(f"/memory/{name}"):
            return record
    return None


def sha_of(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def spread(trio: Trio, name: str, text: str) -> None:
    """The Mac publishes a file and both Linux machines pull it (a base on each)."""
    trio.write(trio.mac, name, text)
    trio.mac.push()
    for machine in (trio.linux, trio.linux2):
        machine.pull()
        assert trio.text(machine, name) == text


def test_the_fleet_dirs_name_one_project(trio):
    """Three folders (one name in the home form), one wire name."""
    names = {name: d.parent.name for name, d in trio.dirs.items()}
    if trio.kind == "home":
        assert names["linux"] == names["linux2"] == "-home-kb-scratch"
        assert names["mac"] == "-Users-kb-scratch"
    else:
        assert len(set(names.values())) == 3


# ── edit, fast-forward, conflict, union ───────────────────────────────


def test_a_peer_edit_to_an_untouched_file_fast_forwards_and_a_deleted_line_stays_deleted(trio):
    spread(trio, "MEMORY.md", "- a\n- b\n- c\n")
    trio.write(trio.mac, "MEMORY.md", "- a\n- c\n")  # a line is deleted
    trio.mac.push()
    for machine in (trio.linux, trio.linux2):
        result = machine.pull()
        assert (result.total_written, result.total_merged, result.total_conflicted) == (1, 0, 0)
        assert trio.text(machine, "MEMORY.md") == "- a\n- c\n"  # no union put "- b" back
        assert not list(machine.claude.rglob("*sync-conflict*"))
    # Pulling again has nothing to say.
    assert trio.linux.pull().total_applied == 0


def test_both_sides_changing_a_file_writes_a_conflict_copy_not_an_overwrite(trio):
    spread(trio, "topic.md", "one\n")
    trio.write(trio.mac, "topic.md", "mac says\n")
    trio.mac.push()
    trio.write(trio.linux, "topic.md", "linux says\n")
    result = trio.linux.pull()
    assert result.total_conflicted == 1 and result.total_merged == 0
    assert trio.text(trio.linux, "topic.md") == "linux says\n"  # local bytes stay canonical
    copies = list(trio.dirs["linux"].glob("topic.sync-conflict-*.md"))
    assert [c.read_text() for c in copies] == ["mac says\n"]
    # A conflict that is already kept is no new outcome (criterion 10).
    again = trio.linux.pull()
    assert again.total_applied == 0 and again.total_conflicted == 0
    assert len(list(trio.dirs["linux"].glob("topic.sync-conflict-*.md"))) == 1


def test_a_mergeable_file_both_sides_changed_unions_and_a_deleted_line_returns(trio):
    """Union has no base bytes: a line one side deleted comes back when the other side also
    changed the file. Pinned as the documented limit of the rule (owner item h)."""
    spread(trio, "MEMORY.md", "- a\n- b\n- c\n")
    trio.write(trio.mac, "MEMORY.md", "- a\n- c\n")  # the Mac deletes "- b"
    trio.mac.push()
    trio.write(trio.linux, "MEMORY.md", "- a\n- b\n- d\n")  # Linux drops "- c" and adds "- d"
    result = trio.linux.pull()
    assert result.total_merged == 1 and result.total_conflicted == 0
    merged = trio.text(trio.linux, "MEMORY.md")
    assert merged is not None
    assert sorted(line for line in merged.splitlines() if line) == ["- a", "- b", "- c", "- d"]


# ── deletion: one record, followers, recovery ─────────────────────────


def test_a_deletion_is_followed_with_one_record_and_a_recovery_copy(trio):
    from mind_meld.cli import _sync_success

    spread(trio, "x.md", "v0\n")
    trio.path(trio.mac).unlink()
    trio.mac.push()
    record = tombstone_for(trio.mac.published())
    assert record is not None and record["sha256"] == sha_of("v0\n")
    assert record["device_id"] == trio.mac.device_id

    for follower in (trio.linux, trio.linux2):
        result = follower.pull()
        assert result.total_deleted == 1 and result.total_failed == 0, result
        assert trio.text(follower) is None
        assert result.deleted_origins == ["mac"]
        copies = [c for c in deleted_copies(follower) if c.name == "x.md"]
        assert [c.read_text() for c in copies] == ["v0\n"]
        assert copies[0].stat().st_mode & 0o777 == 0o600
        # The pull says what it deleted and where the copy is (O8).
        line = _sync_success("pull", result)
        assert line.startswith("Pull: 1 file deleted because mac deleted it; copies in ")
        assert "for 30 days" in line and "#recovering-a-deleted-file" in line

    # One record: a follower mints nothing, not even when its next push publishes.
    trio.write(trio.linux, "other.md", "other\n")
    trio.linux.push()
    assert tombstone_for(trio.linux.published()) is None
    trio.write(trio.linux2, "other2.md", "other2\n")
    trio.linux2.push()
    assert tombstone_for(trio.linux2.published()) is None
    # ...and the third machine's pulls of the followers change nothing.
    assert trio.mac.pull().total_deleted == 0
    assert trio.text(trio.mac) is None


def test_a_copy_put_back_on_the_origin_and_pushed_brings_the_file_back_everywhere(trio):
    spread(trio, "x.md", "v0\n")
    trio.path(trio.mac).unlink()
    trio.mac.push()
    trio.linux.pull()
    trio.linux2.pull()
    [copy] = [c for c in deleted_copies(trio.linux) if c.name == "x.md"]

    # The recovery recipe: copy the file onto the origin (not onto a follower) and push.
    trio.path(trio.mac).write_bytes(copy.read_bytes())
    trio.mac.push()
    assert tombstone_for(trio.mac.published()) is None  # the record ends with the file's return
    for follower in (trio.linux, trio.linux2):
        result = follower.pull()
        assert result.total_written == 1, result
        assert trio.text(follower) == "v0\n"
    assert trio.mac.pull().total_deleted == 0


def test_the_plan_sequence_b_deletes_c_follows_b_restores_c_pushes_b_pulls(trio):
    """B deletes X and pushes, C pulls and follows, B restores X, pushes and pulls, C pushes,
    B pulls: X stays on B and returns to C on C's next pull."""
    b, c = trio.mac, trio.linux
    spread(trio, "x.md", "v0\n")
    trio.path(b).unlink()
    b.push()
    assert c.pull().total_deleted == 1
    assert trio.text(c) is None

    trio.write(b, "x.md", "v0\n")  # restored: same bytes, a fresh logical mtime
    b.push()
    assert tombstone_for(b.published()) is None
    assert b.pull().total_deleted == 0

    trio.write(c, "other.md", "c edits something else\n")
    c.push()
    assert tombstone_for(c.published()) is None  # C followed; it never minted a second record
    result = b.pull()
    assert result.total_deleted == 0
    assert trio.text(b) == "v0\n"  # stays on B

    result = c.pull()
    assert result.total_written == 1
    assert trio.text(c) == "v0\n"  # returns to C


@pytest.mark.parametrize("old_mtime", [False, True], ids=["fresh-mtime", "old-mtime"])
def test_an_origin_restore_reaches_a_follower_even_with_an_old_mtime(trio, old_mtime):
    """`mv`, `cp -p` or Finder's Put Back can keep the old mtime; the follower's row is
    stamped with the record's device, so the restore still propagates."""
    spread(trio, "x.md", "v0\n")
    kept_mtime = trio.path(trio.mac).stat().st_mtime
    trio.path(trio.mac).unlink()
    trio.mac.push()
    trio.linux.pull()
    assert trio.text(trio.linux) is None
    trio.mac.pull()
    restored = trio.path(trio.mac)
    restored.write_text("v0\n")
    if old_mtime:
        os.utime(restored, (kept_mtime, kept_mtime))
    trio.mac.push()
    result = trio.linux.pull()
    assert result.total_written == 1, result
    assert trio.text(trio.linux) == "v0\n"
    ledger = ledger_of(trio.linux, trio.mac)
    assert ledger.newest_stamp(
        "claude", f"projects/{trio.dirs['linux'].parent.name}/memory/x.md"
    ) in (
        None,
        trio.mac.device_id,
    )


def test_a_failed_fsync_after_a_delete_does_not_delete_a_restored_file_again(trio, monkeypatch):
    spread(trio, "x.md", "v0\n")
    trio.path(trio.mac).unlink()
    trio.mac.push()
    real = fsutil.fsync_dir
    claude = str(trio.linux.claude)

    def flaky(path):
        if str(path).startswith(claude):
            raise OSError(5, "injected directory flush failure")
        return real(path)

    monkeypatch.setattr(fsutil, "fsync_dir", flaky)
    result = trio.linux.pull()
    monkeypatch.undo()
    assert result.total_deleted == 1 and result.durability_fsync_failures >= 1, result
    assert trio.text(trio.linux) is None

    # The file is put back by hand; the failed flush must not have kept the base that
    # authorized the first deletion.
    trio.write(trio.linux, "x.md", "v0\n")
    again = trio.linux.pull()
    assert again.total_deleted == 0 and again.total_failed == 0, again
    assert trio.text(trio.linux) == "v0\n"


def test_a_hand_deleted_file_is_not_restored_by_the_next_pull_and_then_propagates(trio):
    """Owner item (g): a pull does not restore a file this machine held, published and removed
    by hand; the removal reaches the fleet at its next push, with a copy on the followers."""
    spread(trio, "x.md", "v0\n")
    trio.linux.push()  # the machine's own manifest now lists x.md: it provably held it
    trio.mac.pull()  # the others match that listing: each now has a base for linux
    trio.linux2.pull()
    trio.path(trio.linux).unlink()
    result = trio.linux.pull()
    assert result.total_written == 0
    assert trio.text(trio.linux) is None
    assert trio.text(trio.mac) == "v0\n"
    trio.linux.push()
    record = tombstone_for(trio.linux.published())
    assert record is not None and record["sha256"] == sha_of("v0\n")
    assert trio.mac.pull().total_deleted == 1
    assert trio.linux2.pull().total_deleted == 1
    assert trio.text(trio.mac) is None and trio.text(trio.linux2) is None


def test_a_machine_with_no_base_for_the_deleting_peer_keeps_the_file(trio):
    """The sync.md limit: a machine that caught up and deletes before its peers have matched
    its listing publishes a hash their base for it does not carry; they keep the file."""
    spread(trio, "x.md", "v0\n")
    trio.linux.push()
    trio.path(trio.linux).unlink()
    trio.linux.push()
    assert tombstone_for(trio.linux.published()) is not None
    result = trio.mac.pull()  # never matched linux's listing: no base for linux
    assert result.total_deleted == 0
    assert trio.text(trio.mac) == "v0\n"
    # Nothing is lost: the file is kept, and the machine that originated it is untouched.
    trio.mac.push()
    assert tombstone_for(trio.mac.published()) is None


@pytest.mark.parametrize("full_hash_known", [True, False], ids=["known", "unknown"])
def test_a_machine_that_pulls_newer_bytes_then_deletes_publishes_that_versions_hash(
    trio, monkeypatch, full_hash_known
):
    from mind_meld import cli

    spread(trio, "x.md", "v0\n")
    trio.linux.push()
    trio.mac.pull()
    trio.write(trio.mac, "x.md", "v1\n")
    trio.mac.push()
    trio.linux.pull()
    assert trio.text(trio.linux) == "v1\n"
    trio.path(trio.linux).unlink()
    if not full_hash_known:
        # Bases can be lost while the prefix-only held history survives.
        ledger = ledger_of(trio.linux, trio.mac, trio.linux2)
        rel = trio.path(trio.linux).relative_to(trio.linux.claude).as_posix()
        ledger.drop("claude", rel)
        with trio.linux.on():
            assert core.commit_ledger(ledger) is None
        assert ledger.newest_held("claude", rel) == sha_of("v1\n")[:16]

    generated = []
    real_generate = cli.generate_tombstones

    def capture(*args, **kwargs):
        records = real_generate(*args, **kwargs)
        generated.append(records)
        return records

    canonical = Path(trio.linux.store) / manifest_key(trio.linux.device_id)
    before = canonical.read_bytes()
    anchor = trio.linux.claude / "projects" / manifest.ANCHOR_NAME
    anchor_before = anchor.read_bytes()
    with monkeypatch.context() as preview_patch:
        preview_patch.setattr(cli, "generate_tombstones", capture)
        trio.linux.push(dry_run=True)
    predicted = tombstone_for({"tombstones": generated[-1]})
    assert canonical.read_bytes() == before
    assert anchor.read_bytes() == anchor_before
    if full_hash_known:
        assert predicted is not None and predicted["sha256"] == sha_of("v1\n")
    else:
        assert predicted is None

    trio.linux.push()
    record = tombstone_for(trio.linux.published())
    if full_hash_known:
        assert record is not None and record["sha256"] == sha_of("v1\n")
    else:
        assert record is None
    # Mac's base for Linux is still v0, so the latest bytes remain safe there.
    assert trio.mac.pull().total_deleted == 0
    assert trio.text(trio.mac) == "v1\n"


def test_a_file_removed_before_this_machine_ever_published_it_is_restored_by_a_pull(trio):
    """Nothing proves this machine kept the file (its own manifest never listed it), so a
    pull brings it back: the safe side of the rule."""
    spread(trio, "x.md", "v0\n")
    trio.path(trio.linux).unlink()
    assert trio.linux.pull().total_written == 1
    assert trio.text(trio.linux) == "v0\n"


# ── edit versus delete, dormant peers ─────────────────────────────────


def expire(machine: Machine, days: int = 31) -> None:
    """Age every deletion record in the machine's published manifest past its 30 days."""
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def age(data: dict) -> None:
        for record in data["tombstones"].values():
            record["deleted_at"] = when

    machine.edit_published(age)


def test_when_one_machine_deletes_what_another_edited_the_edit_survives_and_arrives_later(trio):
    spread(trio, "x.md", "v0\n")
    trio.write(trio.linux, "x.md", "v1 edited on linux\n")
    trio.linux.push()
    trio.path(trio.mac).unlink()  # the Mac deletes the old version
    trio.mac.push()

    # The machine holding the old bytes deletes its copy.
    assert trio.linux2.pull().total_deleted == 1
    assert trio.text(trio.linux2) is None
    # The machine holding the edit keeps it: its bytes are not the deleted version.
    result = trio.linux.pull()
    assert result.total_deleted == 0
    assert trio.text(trio.linux) == "v1 edited on linux\n"
    # The record suppresses the edit on the third machine for as long as it is active.
    assert trio.linux2.pull().total_written == 0
    assert trio.text(trio.linux2) is None

    expire(trio.mac)
    result = trio.linux2.pull()
    assert result.total_written == 1
    assert trio.text(trio.linux2) == "v1 edited on linux\n"


def test_a_machine_that_never_held_a_deleted_file_can_receive_it_from_a_dormant_peer(trio):
    """Owner item (k): after the record's 30 days a dormant peer that still lists the file
    brings it to a machine that never held it. Never lost; removing a dead machine stays an
    owner step."""
    trio.write(trio.mac, "x.md", "v0\n")
    trio.mac.push()
    trio.linux.pull()
    trio.linux.push()  # linux lists x.md, then goes dormant
    trio.mac.pull()
    trio.path(trio.mac).unlink()
    trio.mac.push()
    # linux2 joins late: the record keeps the dormant peer's copy away while it is active.
    assert trio.linux2.pull().total_written == 0
    assert trio.text(trio.linux2) is None

    expire(trio.mac)
    result = trio.linux2.pull()
    assert result.total_written == 1 and result.total_deleted == 0
    assert trio.text(trio.linux2) == "v0\n"
    assert trio.text(trio.linux) == "v0\n"  # the dormant peer's copy was never touched


def test_a_dormant_peer_returning_after_the_record_expired_keeps_its_file(trio):
    spread(trio, "x.md", "v0\n")
    trio.path(trio.mac).unlink()
    trio.mac.push()
    expire(trio.mac)  # linux2 sleeps through the whole 30 days
    result = trio.linux2.pull()
    assert result.total_deleted == 0
    assert trio.text(trio.linux2) == "v0\n"


# ── the stale handoff and the older edit ──────────────────────────────


@pytest.mark.parametrize("a_pulls_first", [True, False], ids=["a-first", "b-first"])
def test_the_two_machine_stale_flow_neither_conflicts_nor_rolls_back(trio, a_pulls_first):
    """A edits, B catches up and publishes A's own older version, A edits again before pulling.
    Neither the stale copy nor the newer edit may read as a conflict, whichever pulls first."""
    a, b = trio.mac, trio.linux
    spread(trio, "MEMORY.md", "- one\n- two\n")
    trio.write(a, "MEMORY.md", "- one\n")  # A deletes a line (V1)
    a.push()
    assert b.pull().total_written == 1  # B fast-forwards to V1 and publishes it
    b.push()
    trio.write(a, "MEMORY.md", "- one\n- three\n")  # A edits again before pulling (V2)
    a.push()

    order = (a, b) if a_pulls_first else (b, a)
    for machine in order:
        result = machine.pull()
        assert result.total_conflicted == 0 and result.total_merged == 0, (machine.name, result)
    assert trio.text(a, "MEMORY.md") == "- one\n- three\n"  # A's V2 is not rolled back
    assert trio.text(b, "MEMORY.md") == "- one\n- three\n"  # B fast-forwards to V2
    assert "- two" not in (trio.text(b, "MEMORY.md") or "")  # and the deleted line stays deleted
    assert not list(trio.mac.claude.rglob("*sync-conflict*"))
    assert not list(trio.linux.claude.rglob("*sync-conflict*"))


def test_a_restored_machine_with_an_older_timestamp_is_held_as_older_until_touched(trio):
    """B builds on the version A holds but its file carries an older mtime (a restored clone):
    A does not apply it, says so, and `touch` plus a push on B ends the hold."""
    a, b = trio.mac, trio.linux
    spread(trio, "x.md", "v0\n")
    trio.write(b, "x.md", "v1 from a restored machine\n", mtime=trio.clock - 3600)
    b.push()
    result = a.pull()
    assert [(e.device_name, e.rel_path.rsplit("/", 1)[-1]) for e in result.older] == [
        ("linux", "x.md")
    ]
    assert trio.text(a) == "v0\n"
    assert result.total_applied == 0
    assert "older peer edit" in _sync_success_line(result)
    # Pulling again keeps saying so and applies nothing.
    assert a.pull().older and trio.text(a) == "v0\n"

    os.utime(trio.path(b), None)  # touch
    b.push()
    result = a.pull()
    assert not result.older and result.total_written == 1
    assert trio.text(a) == "v1 from a restored machine\n"


def _sync_success_line(result) -> str:
    from mind_meld.cli import _sync_success

    return _sync_success("pull", result)


# ── a changed home, hash-less records, same-named files ───────────────


def test_a_push_from_a_device_whose_home_changed_deletes_nothing_on_a_peer(mac, linux):
    """`test_a_changed_home_mints_no_tombstone_and_settles` proves the push side. This carries
    that push through a peer's pull, at the point where a record would start to delete."""
    import shutil

    mac.memory("-Users-kb-loose", "a.md", "a\n")
    mac.push()
    linux.pull()
    landed = linux.claude / "projects/-home-kb-loose/memory/a.md"
    assert landed.read_text() == "a\n"
    linux.push()
    mac.pull()  # each now holds the other's listing: both have a base for the file

    new_home = mac.fs_root / "Users/renamed"
    shutil.move(mac.home, new_home)
    mac.home = new_home
    mac.claude = new_home / ".claude"
    mac.config["sync"]["sources"][0]["path"] = str(mac.claude)
    result = mac.push()
    assert result is not None and result.names_updated and not result.content_changed
    assert mac.published()["tombstones"] == {}

    for _ in range(2):  # and again: nothing settles into a deletion
        pulled = linux.pull()
        assert pulled.total_deleted == 0 and pulled.total_failed == 0, pulled
        assert landed.read_text() == "a\n"
    assert not (linux.state_dir / "deleted").exists()
    linux.push()
    assert linux.published()["tombstones"] == {}


def test_a_hash_less_old_form_record_under_a_same_home_name_deletes_nothing(linux, linux2):
    """Criteria 9 and 20: every record from the release before this one has no content hash.
    It keeps today's effect (a path it names is not downloaded again) and deletes no bytes,
    even on a machine that shares the publisher's home and holds the file unchanged."""
    seed_old_form(
        linux,
        {"-home-kb-scratch/memory/gone.md": "y\n", "-home-kb-scratch/memory/stay.md": "z\n"},
    )
    assert linux2.pull().total_applied == 2
    gone2 = linux2.claude / "projects/-home-kb-scratch/memory/gone.md"
    assert gone2.read_text() == "y\n"

    (linux.claude / "projects/-home-kb-scratch/memory/gone.md").unlink()
    seed_old_form(linux, {})
    linux.as_the_previous_release()
    record = list(linux.published()["tombstones"].values())
    assert len(record) == 1 and "sha256" not in record[0]

    result = linux2.pull()
    assert result.total_deleted == 0 and result.total_failed == 0, result
    assert gone2.read_text() == "y\n"  # the bytes it holds are untouched
    assert not (linux2.state_dir / "deleted").exists()
    # Today's effect remains: with the file gone by hand, the record stops its re-download.
    gone2.unlink()
    assert linux2.pull().total_written == 0
    assert not gone2.exists()


def test_a_same_named_file_with_different_bytes_survives_a_record_for_a_re_cloned_path(mac, linux):
    """The folder name is all a record carries. This path was re-cloned from another
    repository, so the file under the same name holds other bytes: the hash keeps it."""
    mac.memory("-Users-kb-proj", "note.md", "the first repository's note\n")
    mac.push()
    linux.pull()
    note = linux.claude / "projects/-home-kb-proj/memory/note.md"
    assert note.read_text() == "the first repository's note\n"
    linux.push()
    mac.pull()
    # Re-cloned: the folder is rebuilt and the new repository has its own note.md.
    note.write_text("the other repository's note\n")

    (mac.claude / "projects/-Users-kb-proj/memory/note.md").unlink()
    mac.push()
    record = tombstone_for(mac.published(), "note.md")
    assert record is not None and record["sha256"] == sha_of("the first repository's note\n")
    result = linux.pull()
    assert result.total_deleted == 0, result
    assert note.read_text() == "the other repository's note\n"
    verbose = linux.cli("pull", "--dry-run", "--verbose")
    assert verbose.exit_code == 0, verbose.output
    assert "keep (its bytes differ from the deleted version)" in verbose.output


def test_a_same_named_file_in_a_lossy_folder_name_match_survives_a_record(mac, linux):
    """`/Users/kb/my.proj` and `/home/kb/my-proj` encode to one folder name, so the names
    meet though the projects do not. The other project's file holds other bytes."""
    mac.memory("-Users-kb-my-proj", "note.md", "from my.proj on the Mac\n")
    linux_note = linux.memory("-home-kb-my-proj", "note.md", "from my-proj on Linux\n")
    old = time.time() - 3600
    os.utime(linux_note, (old, old))  # older than the Mac's, so the pull keeps both
    mac.push()
    result = linux.pull()  # same name, other bytes: a conflict copy, never an overwrite
    assert result.total_conflicted == 1
    assert linux_note.read_text() == "from my-proj on Linux\n"
    linux.push()
    mac.pull()

    (mac.claude / "projects/-Users-kb-my-proj/memory/note.md").unlink()
    mac.push()
    assert tombstone_for(mac.published(), "note.md") is not None
    result = linux.pull()
    assert result.total_deleted == 0, result
    assert linux_note.read_text() == "from my-proj on Linux\n"
    assert not (linux.state_dir / "deleted").exists()


def test_each_machines_state_db_is_separate(trio):
    spread(trio, "x.md", "v0\n")
    # linux pulled, linux2 pulled, the Mac originated: each keeps its own ledger.
    with trio.linux.on():
        linux_db = statedb.db_path()
    with trio.linux2.on():
        linux2_db = statedb.db_path()
    with trio.mac.on():
        mac_db = statedb.db_path()
    assert len({linux_db, linux2_db, mac_db}) == 3
    assert ledger_of(trio.linux, trio.mac).bases
    assert ledger_of(trio.linux2, trio.mac).bases
    assert not ledger_of(trio.mac, trio.linux, trio.linux2).bases  # the Mac pulled nothing
    # A later pull on one machine adds rows to that machine alone.
    trio.write(trio.mac, "y.md", "v0 of y\n")
    trio.mac.push()
    before = dict(ledger_of(trio.linux2, trio.mac).bases)
    trio.linux.pull()
    assert dict(ledger_of(trio.linux2, trio.mac).bases) == before
    assert len(ledger_of(trio.linux, trio.mac).bases) == len(before) + 1


# ── what a push may call a deletion (criteria 18 and 19) ──────────────


def add_source(machine: Machine, kind: str) -> tuple[Path, str, str]:
    """A second source of ``kind`` with one file; returns (source folder, scan root, file)."""
    root = machine.home / f"{kind}-src"
    if kind == "generic":
        scan, rel, body = "d", "d/n.md", "n\n"
        entry = {"name": "notes", "path": str(root), "type": "generic", "include_dirs": [scan]}
    elif kind == "gstack":
        scan, rel, body = "projects", "projects/proj/design.md", "design\n"
        entry = {"name": "gstack", "path": str(root), "type": "generic", "include_dirs": [scan]}
    elif kind == "muse":
        scan, rel, body = "memory", "memory/personal/MEMORY.md", "- note\n"
        entry = {"name": "muse", "path": str(root), "type": "muse"}
    else:
        raise AssertionError(kind)
    (root / rel).parent.mkdir(parents=True)
    (root / rel).write_text(body)
    machine.config["sync"]["sources"].append(entry)
    return root, scan, rel


def push_line(machine: Machine, **kwargs) -> tuple[PushResult | None, str]:
    """An attended push, and the outcome line it would print."""
    from mind_meld.cli import _sync_success

    with machine.on():
        result = _push_core(
            machine.config,
            PASSPHRASE,
            MEMORY_KB,
            quiet=True,
            attended=True,
            result_observer=lambda r: None,
            **kwargs,
        )
    return result, _sync_success("push", result)


def test_a_tombstone_records_the_hash_of_the_version_that_was_deleted(mac):
    mac.checkout(CHECKOUTS["mac"])
    folder = mac.folder_for(CHECKOUTS["mac"])
    note = mac.memory(folder, "a.md", "first\n")
    mac.push()
    note.write_text("second, the version that is deleted\n")
    mac.push()
    note.unlink()
    result = mac.push()
    assert result is not None and result.total_deleted == 1
    record = tombstone_for(mac.published(), "a.md")
    assert record is not None
    assert record["sha256"] == sha_of("second, the version that is deleted\n")
    assert record["device_id"] == mac.device_id


@pytest.mark.parametrize("kind", ["generic", "gstack", "muse"])
def test_removing_one_file_under_an_unchanged_anchor_mints_with_its_hash(mac, kind):
    root, scan, rel = add_source(mac, kind)
    extra = root / scan / "extra.md"
    extra.write_text("extra\n")
    mac.push()
    (root / rel).unlink()
    mac.push()
    tombstones = mac.published()["tombstones"]
    [key] = [k for k in tombstones if k.endswith(rel)]
    assert tombstones[key]["sha256"] and len(tombstones[key]["sha256"]) == 64


@pytest.mark.parametrize("kind", ["claude", "generic", "gstack", "muse"])
def test_a_recreated_scan_root_mints_nothing_and_says_what_it_left_out(mac, kind):
    import shutil

    if kind == "claude":
        mac.checkout(CHECKOUTS["mac"])
        mac.memory(mac.folder_for(CHECKOUTS["mac"]), "a.md", "a\n")
        folder, scan_root = mac.claude, "projects"
    else:
        folder, scan_root, _ = add_source(mac, kind)
    mac.push()
    shutil.rmtree(folder / scan_root)  # a new empty folder takes its place
    (folder / scan_root).mkdir()
    result, line = push_line(mac)
    assert result is not None and result.total_deleted == 0
    assert mac.published()["tombstones"] == {}
    assert result.left_out_clause.endswith(" is new; run mm pull to restore them.")
    assert result.left_out_clause.startswith("1 file left out because ")
    assert str(folder / scan_root) in result.left_out_clause
    assert line.endswith(result.left_out_clause)
    assert mac.push() is None or mac.published()["tombstones"] == {}


def test_a_restore_that_brings_back_an_older_token_mints_nothing(mac):
    """`rsync -a --delete` of a backup into the existing folder: the folder is the same, the
    file the backup lacked is gone, and the backup's anchor file is back."""
    mac.checkout(CHECKOUTS["mac"])
    folder = mac.folder_for(CHECKOUTS["mac"])
    mac.memory(folder, "a.md", "a\n")
    mac.push()
    anchor = mac.claude / "projects" / manifest.ANCHOR_NAME
    backup_token = anchor.read_bytes()
    newer = mac.memory(folder, "b.md", "made after the backup\n")
    mac.push()
    assert anchor.read_bytes() != backup_token  # every publishing push renews the token
    newer.unlink()  # the restore drops what the backup lacked...
    anchor.write_bytes(backup_token)  # ...and brings its token back
    result, line = push_line(mac)
    assert result is not None and result.total_deleted == 0
    assert mac.published()["tombstones"] == {}
    assert "left out because" in line and "is new" in line


def test_the_first_push_after_upgrading_mints_nothing_once_and_the_next_one_does(mac):
    mac.checkout(CHECKOUTS["mac"])
    folder = mac.folder_for(CHECKOUTS["mac"])
    first = mac.memory(folder, "a.md", "a\n")
    second = mac.memory(folder, "b.md", "b\n")
    mac.push()
    mac.as_the_previous_release()  # the published manifest records no scan-root token
    assert "root_ids" not in mac.published()["sources"]["claude"]

    first.unlink()
    result, line = push_line(mac)
    assert result is not None
    assert mac.published()["tombstones"] == {}  # no token to compare: nothing mints
    assert "left out" not in line  # and the upgrade is silent
    assert mac.published()["sources"]["claude"]["root_ids"].get("projects")
    # The file stays out of the manifest with no record; it can return by a later pull (h).
    assert [rel.rsplit("/", 1)[-1] for rel in mac.published()["sources"]["claude"]["files"]] == [
        "b.md"
    ]

    second.unlink()
    mac.push()
    assert tombstone_for(mac.published(), "b.md") is not None


@pytest.mark.parametrize("kind", ["generic", "gstack", "muse"])
def test_a_hand_seeded_prior_without_tokens_mints_nothing_from_any_source_kind(mac, kind):
    """A prior published by the release before this one records no scan-root token: its first
    push mints nothing from a generic, gstack-style or Muse source; the next one does."""
    root, scan, rel = add_source(mac, kind)
    extra = root / scan / "extra.md"
    extra.write_text("extra\n")
    mac.push()
    mac.as_the_previous_release()
    assert all("root_ids" not in entry for entry in mac.published()["sources"].values())
    (root / rel).unlink()
    mac.push()
    assert mac.published()["tombstones"] == {}
    extra.unlink()
    mac.push()
    [key] = list(mac.published()["tombstones"])
    assert key.endswith("extra.md") and mac.published()["tombstones"][key]["sha256"]


def test_removing_every_project_under_a_live_projects_folder_mints_for_each_file(mac):
    """Named limit: `rm -rf projects/*` leaves the folder and its token, so every file is a
    deletion. Criterion 23 adds no mass-deletion stop; visibility is the guard."""
    import shutil

    mac.checkout(CHECKOUTS["mac"])
    one = mac.folder_for(CHECKOUTS["mac"])
    mac.memory(one, "a.md", "a\n")
    mac.memory(one, "b.md", "b\n")
    mac.memory("-Users-kb-loose", "c.md", "c\n")
    mac.push()
    for child in (mac.claude / "projects").iterdir():
        if child.is_dir():  # the anchor file stays, as it does for `rm -rf projects/*`
            shutil.rmtree(child)
    result = mac.push()
    assert result is not None and result.total_deleted == 3
    assert len(mac.published()["tombstones"]) == 3
    assert all(r["sha256"] for r in mac.published()["tombstones"].values())


def test_the_anchor_file_never_appears_in_a_manifest_and_a_peer_cannot_plant_one(mac, linux):
    mac.checkout(CHECKOUTS["mac"])
    mac.memory(mac.folder_for(CHECKOUTS["mac"]), "a.md", "a\n")
    _, _, _ = add_source(mac, "generic")
    mac.push()
    assert (mac.claude / "projects" / manifest.ANCHOR_NAME).exists()
    for entry in mac.published()["sources"].values():
        assert not [rel for rel in entry["files"] if rel.endswith(manifest.ANCHOR_NAME)]

    # A peer's manifest that lists one is filtered at the consumer boundary.
    planted = f"projects/{WIRE}/memory/{manifest.ANCHOR_NAME}"
    peer = {
        "sources": {"claude": {"files": {planted: {"sha256": "a" * 64}}}},
        "tombstones": {f"claude:{planted}": {"deleted_at": "2026-01-01T00:00:00+00:00"}},
    }
    kept = core._filter_excluded_paths(peer, {}, [])
    assert kept["sources"]["claude"]["files"] == {}
    assert kept["tombstones"] == {}


@pytest.mark.parametrize("shape", ["fifo", "symlink", "oversized", "malformed"])
def test_an_anchor_that_is_not_a_plain_token_file_reads_as_no_token(tmp_path, shape):
    base = tmp_path / "scan"
    base.mkdir()
    target = base / manifest.ANCHOR_NAME
    secret = tmp_path / "elsewhere"
    secret.write_text("a" * 32 + "\n")
    if shape == "fifo":
        os.mkfifo(target)
    elif shape == "symlink":
        target.symlink_to(secret)  # a link must not publish its target's bytes
    elif shape == "oversized":
        target.write_text("a" * 32 + "\n" + "b" * 100)
    else:
        target.write_text("not a token\n")
    outcome = []

    def read():
        outcome.append(manifest.read_anchor(tmp_path, "scan"))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(5)
    assert not reader.is_alive(), "reading the anchor hung"
    assert outcome == [None]
    base_ok = tmp_path / "ok"
    base_ok.mkdir()
    (base_ok / manifest.ANCHOR_NAME).write_text("0123456789abcdef0123456789abcdef\n")
    assert manifest.read_anchor(tmp_path, "ok") == "0123456789abcdef0123456789abcdef"


needs_permissions = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores the permission bits this test relies on"
)


@needs_permissions
def test_an_unwritable_scan_root_has_no_id_and_an_attended_push_says_so_once(mac, capfd):
    mac.checkout(CHECKOUTS["mac"])
    folder = mac.folder_for(CHECKOUTS["mac"])
    note = mac.memory(folder, "a.md", "a\n")
    projects = mac.claude / "projects"
    projects.chmod(0o555)
    try:
        result, _ = push_line(mac)
        assert result is not None and result.unwritable_roots == [str(projects)]
        err = capfd.readouterr().err
        assert err.count("will not propagate") == 1
        assert f"mm: notice: deletions under {projects} will not propagate; " in err
        assert "restore write access, then mm push" in err
        assert "root_ids" not in mac.published()["sources"]["claude"]
        assert not (projects / manifest.ANCHOR_NAME).exists()
    finally:
        projects.chmod(0o755)
    # Nothing under a root that published no id mints: the deletion is not recorded as one.
    note.unlink()
    mac.push()
    assert mac.published()["tombstones"] == {}


@needs_permissions
def test_an_unwritable_scan_root_leaves_a_degraded_breadcrumb_after_autopush(mac):
    mac.checkout(CHECKOUTS["mac"])
    mac.memory(mac.folder_for(CHECKOUTS["mac"]), "a.md", "a\n")
    projects = mac.claude / "projects"
    projects.chmod(0o555)
    try:
        result = mac.cli("autopush")
    finally:
        projects.chmod(0o755)
    assert result.exit_code == 0, result.output
    assert (result.output + (result.stderr or "")).strip() == ""  # a hook prints nothing
    crumb = json.loads((mac.sidecar_dir / "last-autorun.json").read_text())["push"]
    assert crumb["outcome"] == "degraded"
    assert "will not propagate" in crumb["detail"] and "restore write access" in crumb["detail"]


def test_a_deletion_only_push_with_an_unchanged_anchor_publishes(mac):
    mac.checkout(CHECKOUTS["mac"])
    folder = mac.folder_for(CHECKOUTS["mac"])
    mac.memory(folder, "a.md", "a\n")
    gone = mac.memory(folder, "b.md", "b\n")
    mac.push()
    gone.unlink()
    result = mac.push()
    assert result is not None and result.total_deleted == 1 and not result.anchor_updated
    assert tombstone_for(mac.published(), "b.md") is not None
    assert mac.push() is None  # and then it settles


def test_an_anchor_only_change_publishes_metadata_once_and_the_next_push_settles(mac):
    import shutil

    mac.checkout(CHECKOUTS["mac"])
    mac.memory(mac.folder_for(CHECKOUTS["mac"]), "a.md", "a\n")
    mac.push()
    anchor = mac.claude / "projects" / manifest.ANCHOR_NAME
    anchor.unlink()  # the file is lost; every project file is still there
    result, line = push_line(mac)
    assert result is not None and result.anchor_updated and not result.content_changed
    assert line == "Push: metadata updated."  # nothing was left out, so nothing else is said
    assert anchor.exists() and mac.published()["tombstones"] == {}
    assert (
        mac.published()["sources"]["claude"]["root_ids"]["projects"] == anchor.read_text().strip()
    )
    again, line = push_line(mac)
    assert again is None or "metadata updated" not in line
    shutil.rmtree(mac.claude / "projects" / mac.folder_for(CHECKOUTS["mac"]))
    assert mac.push().total_deleted == 1  # the settled anchor lets a real deletion mint again


def test_an_unchanged_upgrade_push_bootstraps_anchors_before_the_first_deletion(mac):
    mac.memory("-Users-kb-scratch", "a.md", "a\n")
    mac.push()
    mac.as_the_previous_release()
    anchor = mac.claude / "projects" / manifest.ANCHOR_NAME
    anchor.unlink()
    result = mac.push()
    assert result is not None and result.anchor_updated
    assert anchor.is_file()
    assert (
        mac.published()["sources"]["claude"]["root_ids"]["projects"] == anchor.read_text().strip()
    )
    assert mac.push() is None


@pytest.mark.parametrize("recovery", ["corrupt", "copies-only", "merged"])
def test_a_recovered_or_merged_anchored_prior_never_mints_new_records(mac, recovery):
    source = mac.home / "notes"
    (source / "d").mkdir(parents=True)
    mac.config["sync"]["sources"] = [
        {"name": "notes", "path": str(source), "type": "generic", "include_dirs": ["d"]}
    ]
    for name in ("first", "second", "kept"):
        (source / "d" / f"{name}.md").write_text(name)
    mac.push()
    (source / "d/first.md").unlink()
    mac.push()
    carried = mac.published()["tombstones"]["notes:d/first.md"]
    (source / "d/second.md").unlink()
    canonical = Path(mac.store) / manifest_key(mac.device_id)
    if recovery != "corrupt":
        canonical.with_name("manifest.json 2.enc").write_bytes(canonical.read_bytes())
    if recovery == "corrupt":
        canonical.write_bytes(b"corrupt")
    elif recovery == "copies-only":
        canonical.unlink()
    mac.push()
    assert mac.published()["tombstones"] == {"notes:d/first.md": carried}


def test_a_push_with_an_unreadable_ledger_publishes_no_new_user_deletions(mac, monkeypatch):
    source = mac.home / "notes"
    (source / "d").mkdir(parents=True)
    mac.config["sync"]["sources"] = [
        {"name": "notes", "path": str(source), "type": "generic", "include_dirs": ["d"]}
    ]
    note = source / "d/note.md"
    note.write_text("a")
    mac.push()
    note.unlink()

    def unavailable(_peers):
        return pullplan.Ledger(readable=False), OSError("isolated unavailable ledger")

    monkeypatch.setattr(core, "load_ledger", unavailable)
    mac.push()
    assert mac.published()["tombstones"] == {}


def test_left_out_files_name_a_missing_folder_and_a_pull_restores_them(mac, linux):
    import shutil

    a = mac.checkout(CHECKOUTS["mac"])
    folder = mac.folder_for(CHECKOUTS["mac"])
    mac.memory(folder, "a.md", "a\n")
    mac.memory(folder, "b.md", "b\n")
    mac.push()
    linux.checkout(CHECKOUTS["linux"])
    (linux.claude / "projects" / linux.folder_for(CHECKOUTS["linux"])).mkdir(parents=True)
    linux.pull()
    linux.push()
    mac.pull()

    shutil.rmtree(mac.claude / "projects")  # the folder is missing (an unmounted volume)
    result, line = push_line(mac)
    projects = mac.claude / "projects"
    assert result is not None and result.total_deleted == 0
    assert result.left_out_clause == (
        f"2 files left out because {projects} is missing; run mm pull to restore them."
    )
    assert line.endswith(result.left_out_clause)
    assert mac.published()["tombstones"] == {}
    mac.cwd = a
    assert mac.pull().total_written == 2


@needs_permissions
def test_left_out_files_name_an_unreadable_folder_and_do_not_say_to_pull(tmp_path):
    """The guard's third reason. A real push refuses before this (the snapshot cannot
    list the folder), so it is pinned where the guard is, with the real permission bits."""
    base = tmp_path / "notes-src"
    (base / "d").mkdir(parents=True)
    anchor_value = "0123456789abcdef0123456789abcdef"
    manifest.write_anchor(base, "d", anchor_value)
    cfg = {"name": "notes", "path": str(base), "type": "generic", "include_dirs": ["d"]}
    prior = {
        "sources": {
            "notes": {"files": {"d/n.md": {"sha256": "a" * 64}}, "root_ids": {"d": anchor_value}}
        }
    }
    local = {"sources": {"notes": {"files": {}}}}
    ledger = pullplan.Ledger()
    try:
        assert (
            core.compute_mint_guard(local, prior, [cfg], ledger).left_out == {}
        )  # readable: a deletion
        (base / "d").chmod(0o000)
        guard = core.compute_mint_guard(local, prior, [cfg], ledger)
    finally:
        (base / "d").chmod(0o755)
    assert guard.skip == {("notes", "d/n.md")} and guard.left_out == {"notes": {"d/n.md"}}
    assert guard.clause() == (
        f"1 file left out because {base / 'd'} cannot be read; restore read access."
    )
    assert "mm pull" not in guard.clause()


def test_status_and_a_dry_run_do_not_count_left_out_files_as_deleted(mac):
    import shutil

    mac.checkout(CHECKOUTS["mac"])
    folder = mac.folder_for(CHECKOUTS["mac"])
    mac.memory(folder, "a.md", "a\n")
    mac.push()
    shutil.rmtree(mac.claude / "projects")
    (mac.claude / "projects").mkdir()  # recreated empty: a new folder
    for args in (("status",), ("push", "--dry-run")):
        out = mac.cli(*args)
        assert out.exit_code == 0, out.output
        assert "deleted" not in out.output.replace("Nothing to push", ""), (args, out.output)
    before = LocalBackend(mac.store).get(manifest_key(mac.device_id))
    assert mac.cli("push", "--dry-run").exit_code == 0
    assert LocalBackend(mac.store).get(manifest_key(mac.device_id)) == before
    assert not (mac.claude / "projects" / manifest.ANCHOR_NAME).exists()  # previews only read


def _orphan_blob(machine: Machine) -> str:
    key = storage_keys.blob_key(machine.device_id, "f" * 64)
    LocalBackend(machine.store).put(key, b"an orphan a GC would collect")
    return key


@pytest.mark.parametrize("left_out", [False, True], ids=["control", "files-left-out"])
def test_post_push_gc_holds_while_files_are_left_out(mac, left_out):
    """A push that left files out dropped their references without seeing them. Its sweep
    would collect the only store copy of a file a pull could still restore, so it waits for
    a push that lists them. The control proves a content push does sweep."""
    import shutil

    notes, scan, rel = add_source(mac, "generic")
    mac.checkout(CHECKOUTS["mac"])
    note = mac.memory(mac.folder_for(CHECKOUTS["mac"]), "a.md", "a\n")
    mac.push()
    sha = mac.published()["sources"]["claude"]["files"][f"projects/{WIRE}/memory/a.md"]["sha256"]
    held_blob = storage_keys.blob_key(mac.device_id, sha)
    orphan = _orphan_blob(mac)
    backend = LocalBackend(mac.store)
    assert backend.exists(held_blob)
    if left_out:
        shutil.rmtree(mac.claude / "projects")
        (mac.claude / "projects").mkdir()  # new: a.md is left out, not deleted
    else:
        note.unlink()  # a real deletion under an unchanged anchor
    (notes / rel).write_text("a content change, so the push sweeps\n")
    result = mac.cli("push")
    assert result.exit_code == 0, result.output
    assert backend.exists(held_blob) is left_out
    assert backend.exists(orphan) is left_out


# ── a pull restores what a push left out ──────────────────────────────


def _left_out_project(mac: Machine, linux: Machine, kind: str):
    """The Mac's project is on a peer too; the Mac's `projects/` is then recreated empty."""
    import shutil

    if kind == "git":
        checkout = mac.checkout(CHECKOUTS["mac"])
        folder = mac.folder_for(CHECKOUTS["mac"])
        linux.checkout(CHECKOUTS["linux"])
        peer_folder = linux.folder_for(CHECKOUTS["linux"])
        (linux.claude / "projects" / peer_folder).mkdir(parents=True)
    else:
        checkout = None
        folder = "-Users-kb-scratch"
    files = [mac.memory(folder, "a.md", "a\n"), mac.memory(folder, "b.md", "b\n")]
    mac.push()
    linux.pull()
    linux.push()
    shutil.rmtree(mac.claude / "projects")
    (mac.claude / "projects").mkdir()
    return checkout, folder, files


@pytest.mark.parametrize("kind", ["git", "home"])
def test_a_pull_outside_the_checkout_restores_the_files_a_push_left_out(mac, linux, kind):
    _, folder, files = _left_out_project(mac, linux, kind)
    result, line = push_line(mac)
    assert result is not None and result.total_deleted == 0
    assert result.left_out_clause.startswith("2 files left out because ")
    assert mac.published()["tombstones"] == {}
    assert mac.cwd is None  # the pull runs from the home folder, outside any checkout
    restored = mac.pull()
    assert restored.total_written == 2, restored
    assert [f.read_text() for f in files] == ["a\n", "b\n"]


def test_after_a_further_content_push_a_git_project_restores_only_inside_its_checkout(mac, linux):
    """The documented limit: the folder owners a left-out push keeps last one publish."""
    checkout, folder, files = _left_out_project(mac, linux, "git")
    notes, scan, rel = add_source(mac, "generic")
    push_line(mac)  # leaves the files out and keeps the project's folder owner
    (notes / rel).write_text("a content change\n")
    mac.push()  # a further content push, with no pull between
    assert not any(f.exists() for f in files)

    assert mac.pull().total_written == 0  # from the home folder the project is unplaced
    assert not any(f.exists() for f in files)
    mac.cwd = checkout
    assert mac.pull().total_written == 2
    assert [f.read_text() for f in files] == ["a\n", "b\n"]


def test_an_outage_with_no_push_is_restored_by_a_pull_and_mints_nothing(mac, linux):
    """`projects/` is missing and no push runs; a pull recreates it. The push after that
    finds a folder with another token and reads nothing under it as deleted."""
    import shutil

    checkout, folder, files = _left_out_project(mac, linux, "git")
    shutil.rmtree(mac.claude / "projects")  # missing again, as at the outage
    mac.cwd = checkout
    assert mac.pull().total_written == 2
    assert [f.read_text() for f in files] == ["a\n", "b\n"]
    mac.push()
    assert mac.published()["tombstones"] == {}
    assert mac.pull().total_applied == 0


def test_a_restore_waiting_on_a_blob_is_not_minted_as_a_deletion(mac, linux):
    """The only route for a never-restored file into a mint: it was held and published, then
    lost in an outage, and the peer's blob is not here yet. The push must not tombstone it."""
    checkout, folder, files = _left_out_project(mac, linux, "git")
    backend = LocalBackend(mac.store)
    for key in backend.list_keys("data/"):
        backend.delete(key)  # no blob is reachable: the restore waits
    mac.cwd = checkout
    waiting = mac.pull()
    assert waiting.total_written == 0 and waiting.total_waiting == 2, waiting
    assert not any(f.exists() for f in files)
    mac.push()
    assert mac.published()["tombstones"] == {}


def test_a_peers_file_that_was_never_delivered_here_mints_nothing(mac, linux):
    mac.checkout(CHECKOUTS["mac"])
    mac.memory(mac.folder_for(CHECKOUTS["mac"]), "a.md", "a\n")
    mac.push()
    linux.checkout(CHECKOUTS["linux"])
    folder = linux.folder_for(CHECKOUTS["linux"])
    linux.memory(folder, "only-on-linux.md", "never reaches the Mac\n")
    linux.push()
    # The Mac never pulled it, so it is not in the Mac's prior; its next pushes mint nothing.
    mac.memory(mac.folder_for(CHECKOUTS["mac"]), "b.md", "b\n")
    mac.push()
    assert mac.published()["tombstones"] == {}
    assert not any("only-on-linux" in rel for rel in mac.published()["sources"]["claude"]["files"])


# ── a followed deletion, hardcoded exclusions, ancestry ───────────────


def test_a_path_the_pull_deleted_mints_nothing_and_the_stamp_clears_when_published_again(trio):
    spread(trio, "x.md", "v0\n")
    trio.linux.push()  # linux's own manifest now lists x.md
    trio.mac.pull()
    trio.path(trio.mac).unlink()
    trio.mac.push()
    assert trio.linux.pull().total_deleted == 1
    rel = f"projects/{trio.dirs['linux'].parent.name}/memory/x.md"
    assert ledger_of(trio.linux, trio.mac).newest_stamp("claude", rel) == trio.mac.device_id

    # x.md is in linux's prior and absent on disk, but linux only followed the deletion.
    trio.write(trio.linux, "other.md", "something else changed\n")
    trio.linux.push()
    assert tombstone_for(trio.linux.published()) is None
    assert ledger_of(trio.linux, trio.mac).newest_stamp("claude", rel) == trio.mac.device_id

    # Published again, the stamp goes; a later hand deletion mints as any other.
    trio.write(trio.linux, "x.md", "v0\n")
    trio.linux.push()
    assert ledger_of(trio.linux, trio.mac).newest_stamp("claude", rel) is None
    trio.path(trio.linux).unlink()
    trio.linux.push()
    assert tombstone_for(trio.linux.published()) is not None


def test_a_file_still_on_disk_that_only_the_hardcoded_exclusions_omit_mints_nothing(mac):
    root, scan, rel = add_source(mac, "generic")
    cache = root / scan / "module.pyc"  # "*.pyc" is excluded by mm, whatever the source says
    cache.write_bytes(b"compiled")
    mac.push()

    def pretend_published(data: dict) -> None:
        data["sources"]["notes"]["files"][f"{scan}/module.pyc"] = {
            "sha256": sha_of("compiled"),
            "size": 8,
            "mtime": "2026-01-01T00:00:00+00:00",
        }

    mac.edit_published(pretend_published)  # a release that did not exclude it listed it
    (root / rel).write_text("a change, so the push publishes\n")
    mac.push()
    assert mac.published()["tombstones"] == {}
    assert cache.exists()


def prefix16(text: str) -> str:
    return sha_of(text)[:16]


def test_a_published_file_lists_the_versions_it_was_built_on_newest_first(mac):
    mac.checkout(CHECKOUTS["mac"])
    folder = mac.folder_for(CHECKOUTS["mac"])
    note = mac.memory(folder, "a.md", "v0\n")
    key = f"projects/{WIRE}/memory/a.md"

    def entry() -> dict:
        return mac.published()["sources"]["claude"]["files"][key]

    mac.push()
    assert "from" not in entry()
    note.write_text("v1\n")
    mac.push()
    assert entry()["from"] == [prefix16("v0\n")]
    note.write_text("v2\n")
    mac.push()
    assert entry()["from"] == [prefix16("v1\n"), prefix16("v0\n")]

    # An unchanged entry keeps the list it published, even when other files publish.
    mac.memory(folder, "b.md", "b\n")
    mac.push()
    assert entry()["from"] == [prefix16("v1\n"), prefix16("v0\n")]


def test_a_files_ancestry_is_capped_at_sixteen_versions(mac):
    mac.checkout(CHECKOUTS["mac"])
    folder = mac.folder_for(CHECKOUTS["mac"])
    note = mac.memory(folder, "a.md", "v0\n")
    key = f"projects/{WIRE}/memory/a.md"
    mac.push()
    for n in range(1, 21):
        note.write_text(f"v{n}\n")
        mac.push()
    chain = mac.published()["sources"]["claude"]["files"][key]["from"]
    assert len(chain) == 16
    assert chain[0] == prefix16("v19\n") and chain[-1] == prefix16("v4\n")


def test_the_internal_usage_source_publishes_no_ancestry():
    ledger = pullplan.Ledger(untracked=frozenset({"mm-events"}))
    ledger.hold("mm-events", "events/2026-11-01.jsonl", "a" * 64)
    ledger.hold("notes", "n.md", "b" * 64)
    local = {
        "sources": {
            "mm-events": {"files": {"events/2026-11-01.jsonl": {"sha256": "c" * 64}}},
            "notes": {"files": {"n.md": {"sha256": "d" * 64}}},
        }
    }
    core.stamp_ancestry(local, None, ledger)
    assert "from" not in local["sources"]["mm-events"]["files"]["events/2026-11-01.jsonl"]
    assert local["sources"]["notes"]["files"]["n.md"]["from"] == ["b" * 16]


def test_a_push_that_loses_projects_mid_run_mints_nothing_and_a_pull_restores_the_files(
    mac, linux, monkeypatch
):
    """`projects/` goes between the manifest load and the scan: the push sees an empty folder
    it never published, so every file it cannot see is left out, in every project."""
    from mind_meld import cli as cli_module

    checkout, folder, files = _left_out_project(mac, linux, "git")
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f.name.removesuffix(".md") + "\n")  # they are back on disk...
    projects = mac.claude / "projects"
    real = cli_module.build_manifest_v2

    def vanish_then_scan(*args, **kwargs):
        if projects.exists():
            projects.rename(mac.claude / "projects.parked")
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(cli_module, "build_manifest_v2", vanish_then_scan)
        result = mac.push()  # ...and then lost while the push is reading
    assert result is None or result.total_deleted == 0
    assert mac.published()["tombstones"] == {}
    assert len(mac.published()["sources"]["claude"]["files"]) == 2  # nothing was dropped
    (mac.claude / "projects.parked").rename(mac.claude / "projects.lost")
    mac.cwd = checkout
    assert mac.pull().total_written == 2
    assert mac.published()["tombstones"] == {}


def test_a_peer_cannot_plant_an_anchor_file_through_a_pull(mac, linux):
    mac.memory("-Users-kb-scratch", "a.md", "a\n")
    mac.push()

    def plant(data: dict) -> None:
        data["sources"]["claude"]["files"][
            f"projects/@home-scratch/memory/{manifest.ANCHOR_NAME}"
        ] = {"sha256": sha_of("a\n"), "size": 2, "mtime": "2026-01-01T00:00:00+00:00"}

    mac.edit_published(plant)
    result = linux.pull()
    assert result.total_written == 1  # only a.md
    assert not list(linux.claude.rglob(manifest.ANCHOR_NAME))


def test_status_and_a_dry_run_say_what_a_push_would_leave_out(mac):
    """The plan: the owner must act when files are left out, so `mm status` (its verdict) and
    `mm push --dry-run` carry the same reason the push's outcome line does, instead of
    "In sync" beside 0 local files and 1 remote."""
    import shutil

    mac.checkout(CHECKOUTS["mac"])
    mac.memory(mac.folder_for(CHECKOUTS["mac"]), "a.md", "a\n")
    mac.push()
    shutil.rmtree(mac.claude / "projects")
    (mac.claude / "projects").mkdir()
    status = mac.cli("status")
    assert status.exit_code == 0, status.output
    assert "left out because" in first_line(status), first_line(status)
    assert "In sync" not in first_line(status)
    preview = mac.cli("push", "--dry-run")
    assert preview.exit_code == 0, preview.output
    assert "left out because" in " ".join(preview.output.split())


# ── three machines, handoffs and reverts ──────────────────────────────


def test_an_alternating_handoff_fast_forwards_each_side_without_a_conflict(trio):
    """A edits and pushes V1; B pulls it, edits to V2 and pushes without ever publishing the
    version it started from; A pulls: V2 was built on V1, which A holds, so A fast-forwards
    and a line B deleted stays deleted."""
    a, b = trio.mac, trio.linux
    spread(trio, "MEMORY.md", "- one\n- two\n- three\n")
    trio.write(a, "MEMORY.md", "- one\n- two\n- three\n- four\n")  # V1
    a.push()
    b.pull()
    trio.write(b, "MEMORY.md", "- one\n- three\n- four\n")  # V2: B deletes "- two"
    b.push()
    result = a.pull()
    assert (result.total_written, result.total_merged, result.total_conflicted) == (1, 0, 0)
    assert trio.text(a, "MEMORY.md") == "- one\n- three\n- four\n"


@pytest.mark.parametrize("tip_first", [False, True], ids=["chain-first", "tip-first"])
def test_three_machines_with_a_lagging_peer_converge_in_either_pull_order(
    mac, linux, linux2, tip_first
):
    """X edits V1, Y pulls it and edits V2, and Z (still at V0) pulls from both. `chain-first`
    meets V1 then V2 (Z is linux2: mac, linux); `tip-first` meets V2 then V1 (Z is mac: linux,
    then linux2). Neither order may read the older version as a conflict."""
    if tip_first:
        x, y, z = linux2, linux, mac
    else:
        x, y, z = mac, linux, linux2
    trio = Trio("home", mac, linux, linux2)
    trio.write(mac, "MEMORY.md", "- one\n- two\n")
    mac.push()
    for machine in (linux, linux2):
        machine.pull()
        machine.push()  # everyone lists the file: each has a base for every peer
    mac.pull()
    trio.write(x, "MEMORY.md", "- one\n- two\n- five\n")  # V1 adds "- five"
    x.push()
    y.pull()
    trio.write(y, "MEMORY.md", "- one\n- two\n- four\n")  # V2 deletes "- five", adds "- four"
    y.push()

    result = z.pull()
    assert result.total_conflicted == 0 and result.total_merged == 0, result
    # V2, with "- five" still deleted: V1 is V2's own ancestor, not a rival edit.
    assert trio.text(z, "MEMORY.md") == "- one\n- two\n- four\n"
    assert not list(z.claude.rglob("*sync-conflict*"))


def test_an_exact_revert_with_a_fresh_mtime_propagates(trio):
    """Rule 4 before rule 5: A reverts to the bytes of an earlier version. The revert is built
    on the current version (it is in `from`), so B fast-forwards to it even though B has
    held those very bytes before."""
    a, b = trio.mac, trio.linux
    spread(trio, "x.md", "good\n")
    trio.write(a, "x.md", "bad edit\n")
    a.push()
    b.pull()
    assert trio.text(b) == "bad edit\n"
    trio.write(a, "x.md", "good\n")  # exactly the earlier bytes, a fresh mtime
    a.push()
    result = b.pull()
    assert result.total_written == 1 and result.total_conflicted == 0, result
    assert trio.text(b) == "good\n"


@pytest.mark.parametrize("name", ["topic.md", "MEMORY.md"])
def test_inherited_ancestry_survives_two_sequential_handoffs(trio, name):
    """C never held A's V1; its inherited provenance must still reach A via V3."""
    a, b, c = trio.mac, trio.linux2, trio.linux  # A encounters C before B.
    spread(trio, name, "- base\n")
    for machine in (b, c):
        machine.push()
    a.pull()
    trio.write(a, name, "- base\n- removed\n")  # V1
    a.push()
    b.pull(from_device=a.device_id)
    assert trio.text(b, name) == "- base\n- removed\n"
    trio.write(b, name, "- base\n- middle\n")  # V2 removes a line from V1.
    b.push()
    c.pull(from_device=b.device_id)  # C misses A entirely, and only writes V2.
    assert trio.text(c, name) == "- base\n- middle\n"
    trio.write(c, name, "- base\n- final\n")  # V3
    c.push()  # Reloads C's committed database, so an in-memory fix is insufficient.

    preview = a.cli("pull", "--dry-run")
    assert preview.exit_code == 0, preview.output
    result = a.pull()
    assert (result.total_written, result.total_merged, result.total_conflicted) == (1, 0, 0)
    assert trio.text(a, name) == "- base\n- final\n"
    assert "Would write 1, merge up to 0, conflict 0" in preview.output, preview.output
    assert not list(a.claude.rglob("*sync-conflict*"))
    assert a.pull().total_applied == 0
    assert trio.text(a, name) == "- base\n- final\n"


@pytest.mark.parametrize("peer_base_known", [False, True])
@pytest.mark.parametrize("publish_intermediate", [False, True])
def test_a_handoff_exact_revert_propagates_when_the_origin_missed_the_intermediate_push(
    trio, peer_base_known, publish_intermediate
):
    a, b = trio.mac, trio.linux
    spread(trio, "x.md", "good\n")
    b.push()
    if peer_base_known:
        a.pull()  # A's base for B stays at V0 throughout B's later work.
    trio.write(a, "x.md", "bad edit\n")
    a.push()
    b.pull()
    assert trio.text(b) == "bad edit\n"
    if publish_intermediate:
        b.push()  # A does not pull this publication.
    trio.write(b, "x.md", "good\n")
    b.push()

    files = b.published()["sources"]["claude"]["files"]
    entry = next(info for rel, info in files.items() if rel.endswith("/memory/x.md"))
    assert prefix16("bad edit\n") in entry.get("from", [])
    preview = a.cli("pull", "--dry-run")
    assert preview.exit_code == 0, preview.output
    assert "Would write 1, merge up to 0, conflict 0" in preview.output, preview.output
    result = a.pull()
    assert result.total_written == 1 and result.total_conflicted == 0, result
    assert trio.text(a) == trio.text(b) == "good\n"
    a.push()
    for machine in (a, b):
        again = machine.pull()
        assert again.total_written == again.total_merged == again.total_conflicted == 0, again
        assert trio.text(machine) == "good\n"
