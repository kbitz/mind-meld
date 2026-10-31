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
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mind_meld import config as config_module
from mind_meld import core, crypto, devices, manifest, projectid, pullflow, sidecar
from mind_meld.cli import PushResult, _push_core, app
from mind_meld.errors import ManifestError
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
        self.fs_root = tmp_path / f"fs-{name}"
        self.home = self.fs_root / home.strip("/")
        self.claude = self.home / ".claude"
        self.claude.mkdir(parents=True)
        self.sidecar_dir = tmp_path / f"sidecar-{name}"
        self.sidecar_dir.mkdir()
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
        path = self.sidecar_dir / "config.toml"
        config_module.save_config(self.config, path)
        with self.on(), pytest.MonkeyPatch.context() as mp:
            mp.setattr(config_module, "CONFIG_PATH", path)
            mp.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
            return runner.invoke(app, list(args))

    def claim_base_path(self) -> None:
        """Record the publisher's absolute base path as it would read on a real machine.

        The walker records the real path of this machine's Claude folder, which
        lives under a temporary root here; a real machine's is ``/Users/kb/.claude``.
        The envelope's first byte and the rest of the manifest are untouched.
        """
        path = Path(self.store) / manifest_key(self.device_id)
        raw = path.read_bytes()
        with self.on():
            data = json.loads(crypto.decrypt_manifest(raw, PASSPHRASE, MEMORY_KB))
            data["sources"]["claude"]["base_path"] = "/" + str(
                self.claude.relative_to(self.fs_root)
            )
            path.write_bytes(
                crypto.encrypt_manifest(
                    json.dumps(data).encode(),
                    PASSPHRASE,
                    MEMORY_KB,
                    wire_names=raw[0] == crypto.MANIFEST_FORMAT_VERSION,
                )
            )

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
        ("https://x-access-token:SECRET@github.com/acme/widgets.git?token=abc", IDENTITY),
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
    for url in (
        "https://user:hunter2@github.com/acme/widgets.git",
        "ssh://git:hunter2@github.com/acme/widgets.git",
        "https://x-access-token:T0KEN@github.com/acme/widgets",
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
    verdict = lambda **kw: statusflow._status_verdict(  # noqa: E731
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


def test_status_source_other_prints_no_project_name_text(mac):
    mac.checkout("/Users/kb/Dev/widgets")
    folder = mac.folder_for("/Users/kb/Dev/widgets")
    notes = mac.home / "notes-src"
    notes.mkdir()
    (notes / "n.md").write_text("n\n")
    mac.config["sync"]["sources"].append({"name": "notes", "path": str(notes), "type": "generic"})
    seed_old_form(mac, {f"{folder}/memory/MEMORY.md": "a\n"})
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
