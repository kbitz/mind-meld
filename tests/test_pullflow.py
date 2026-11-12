"""CLI-free pull contract for Track 86A.

Setup uses only core/crypto/manifest/devices/storage leaves and conftest's
existing sidecar, history, device-lock and crypto isolation. PASSPHRASE is a
synthetic test credential. Globals deliberately rebound here: pullflow.console,
pullflow.stderr_console, and core._SYNC_OUTPUT (set for attended, unset for
quiet/direct). The summary wrapper records actual output while calling the
original renderer; no pull decisions or storage reads are mocked.

The quiet branch suppresses totals even with _SYNC_OUTPUT unset. A third,
direct/nonquiet context pins the totals with elapsed/MB regex normalization.
Shell exits/outcomes, corrupt-peer and exception-observer behavior remain in
the retargeted shell suites; these tests pin module results and summary channels.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from contextlib import redirect_stderr
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from rich.console import Console

from mind_meld import (
    core,
    crypto,
    devices,
    errors,
    manifest,
    pullflow,
    pullhistory,
    pullplan,
    resolveflow,
    sidecar,
    statedb,
)
from mind_meld.storage.keys import blob_key, device_key, manifest_key
from mind_meld.storage.local import LocalBackend
from tests.syncthing_folder import SyncthingFolder

PASSPHRASE = "pullflow-contract-test-only"
MEMORY_KB = 1024
LOCAL = "aaaaaaaa"
PEER = "bbbbbbbb"
MODES = ("attended", "quiet", "direct")


@pytest.fixture
def fleet(tmp_path):
    local = tmp_path / "local"
    peer = tmp_path / "peer"
    local.mkdir()
    peer.mkdir()
    backend = LocalBackend(tmp_path / "storage")
    boot = crypto.bootstrap_crypto_init(backend, PASSPHRASE, MEMORY_KB)
    crypto.set_crypto_session(boot.root_salt, MEMORY_KB)
    for did, name in ((LOCAL, "Local A"), (PEER, "Peer B")):
        devices.register_device(backend, did, name)
        devices.update_last_seen(backend, did)
    source = {"name": "notes", "type": "generic", "path": str(local), "include_dirs": ["."]}
    cfg = {
        "device": {"id": LOCAL, "name": "Local A"},
        "storage": {"path": str(backend.root)},
        "sync": {"sources": [source], "max_file_size": 52_428_800},
        "crypto": {"argon2_memory_kb": MEMORY_KB},
    }
    return SimpleNamespace(root=tmp_path, local=local, peer=peer, backend=backend, config=cfg)


def _seed(fleet, files, *, tombstones=None):
    for name, content in files.items():
        path = fleet.peer / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, (1_700_000_000, 1_700_000_000))
    peer = manifest.build_manifest_v2(
        PEER,
        "Peer B",
        [{"name": "notes", "type": "generic", "path": str(fleet.peer), "include_dirs": ["."]}],
    )
    peer["tombstones"] = tombstones or {}
    transferred = 0
    for name, info in peer["sources"]["notes"]["files"].items():
        encrypted = crypto.encrypt((fleet.peer / name).read_bytes(), PASSPHRASE, MEMORY_KB)
        fleet.backend.put(blob_key(PEER, info["sha256"]), encrypted)
        transferred += len(encrypted)
    fleet.backend.put(
        manifest_key(PEER), crypto.encrypt(manifest.serialize_manifest(peer), PASSPHRASE, MEMORY_KB)
    )
    fetched = core._fetch_remote_manifest(fleet.backend, PEER, PASSPHRASE, MEMORY_KB)
    assert fetched.status == "ok" and fetched.manifest == peer
    return peer, transferred


def _normalize(text):
    text = re.sub(r"\b\d+(?:\.\d+)?MB transferred", "<MB>MB transferred", text)
    return re.sub(r"Completed in \d+(?:\.\d+)?s", "Completed in <SECONDS>s", text)


def _run(fleet, monkeypatch, mode, *, refusal=None, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    for binding, stream in (("console", out), ("stderr_console", err)):
        monkeypatch.setattr(
            pullflow,
            binding,
            Console(file=stream, width=160, force_terminal=False, color_system=None),
        )
    real_summary = pullflow._print_pull_summary
    summaries = []

    def record_summary(*args, **options):
        before_out, before_err = len(out.getvalue()), len(err.getvalue())
        real_summary(*args, **options)
        summaries.append(
            (_normalize(out.getvalue()[before_out:]), _normalize(err.getvalue()[before_err:]))
        )

    monkeypatch.setattr(pullflow, "_print_pull_summary", record_summary)
    state = (
        {"verbose": kwargs.get("verbose", False), "diagnostics": [], "progress": None}
        if mode == "attended"
        else None
    )
    token = core._SYNC_OUTPUT.set(state)
    try:
        with redirect_stderr(err):
            result = pullflow._pull_core(
                fleet.config, PASSPHRASE, MEMORY_KB, quiet=mode == "quiet", **kwargs
            )
    finally:
        core._SYNC_OUTPUT.reset(token)
        if refusal is not None:
            refusal.update(summaries=summaries, output=(out.getvalue(), err.getvalue()))
    return result, summaries, (_normalize(out.getvalue()), _normalize(err.getvalue()))


def _counts(result, **expected):
    fields = asdict(result)
    for edit in fields["older"]:
        # Identity/count contracts predate diagnostic mtime metadata. Every
        # actual older verdict must carry the two timestamps it compared.
        local_mtime = datetime.fromisoformat(edit.pop("local_mtime"))
        peer_mtime = datetime.fromisoformat(edit.pop("peer_mtime"))
        assert local_mtime > peer_mtime
    elapsed = fields.pop("elapsed")
    assert elapsed >= 0
    defaults = asdict(pullflow.PullResult())
    defaults.pop("elapsed")
    defaults.update(expected)
    assert fields == defaults
    assert result.total_applied == sum(
        expected.get(k, 0) for k in ("total_written", "total_merged", "total_conflicted")
    )


def _snapshot(root):
    return {
        str(p.relative_to(root)): (
            p.is_dir(),
            p.stat().st_mode,
            p.stat().st_mtime_ns,
            None if p.is_dir() else p.read_bytes(),
        )
        for p in root.rglob("*")
    }


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("scenario", ("clean", "conflict", "newer-local", "tombstone", "merged"))
def test_two_device_pull_counts_and_summary(fleet, monkeypatch, mode, scenario):
    name = "events.jsonl" if scenario == "merged" else "note.md"
    remote = b'{"remote":1}\n' if scenario == "merged" else b"peer content\n"
    tombstones = (
        {"notes:note.md": {"deleted_at": datetime.now(timezone.utc).isoformat(), "device_id": PEER}}
        if scenario == "tombstone"
        else None
    )
    _, transferred = _seed(fleet, {name: remote}, tombstones=tombstones)
    if scenario != "clean":
        (fleet.local / name).write_bytes(
            b'{"local":1}\n' if scenario == "merged" else b"local content\n"
        )
        os.utime(fleet.local / name, (1_600_000_000, 1_600_000_000))
    if scenario == "newer-local":
        # Characterization: Track 91A is expected to replace this mtime rule.
        os.utime(fleet.local / name, (1_800_000_000, 1_800_000_000))
    result, summaries, _ = _run(fleet, monkeypatch, mode)
    counter = {
        "clean": "written",
        "conflict": "conflicted",
        "newer-local": "skipped",
        "merged": "merged",
    }.get(scenario)
    expected = (
        {}
        if counter is None
        else {f"total_{counter}": 1, "bytes_transferred": transferred, "device_names": ["Peer B"]}
    )
    _counts(result, **expected)
    if scenario == "clean":
        assert (fleet.local / name).read_bytes() == remote
    elif scenario == "conflict":
        assert (fleet.local / name).read_bytes() == b"local content\n"
        copies = list(fleet.local.glob("note.sync-conflict-*.md"))
        assert len(copies) == 1 and copies[0].read_bytes() == remote
    elif scenario in {"newer-local", "tombstone"}:
        # Characterization: Track 91A propagates deletions; today a tombstone
        # suppresses the incoming blob while preserving the local canonical.
        assert (fleet.local / name).read_bytes() == b"local content\n"
        assert list(fleet.local.glob("*.sync-conflict-*")) == []
    else:
        assert (fleet.local / name).read_bytes() == b'{"local":1}\n{"remote":1}\n'
    stdout = (
        "  notes: 1 conflicts\n    - note.md\n"
        if scenario == "conflict" and mode != "quiet"
        else ""
    )
    stderr = ""
    if scenario == "conflict":
        if mode == "quiet":
            stderr = "mm: warning: Peer B/notes — 1 conflicts: note.md\n"
        elif mode == "attended":
            stderr = (
                "mm: 1 conflict copy kept; run mm conflicts to review, "
                "mm resolve to pick a winner\n"
            )
    if mode == "direct":
        phrase = {
            "clean": "1 written",
            "conflict": "1 conflicts",
            "newer-local": "1 skipped (local newer)",
            "tombstone": "nothing to apply",
            "merged": "1 merged",
        }[scenario]
        stdout += f"\nPull complete.\n  {phrase}\n"
        if scenario != "tombstone":
            stdout += "  <MB>MB transferred\n"
        stdout += "  Completed in <SECONDS>s\n"
        if scenario == "conflict":
            stdout += "  Run mm conflicts to review, mm resolve to pick a winner.\n"
    assert summaries == [(stdout, stderr)]


@pytest.mark.parametrize("mode", ("attended", "quiet"))
def test_no_peers_returns_without_summary(fleet, monkeypatch, mode):
    fleet.backend.delete(device_key(PEER))
    result, summaries, output = _run(fleet, monkeypatch, mode)
    _counts(result, no_peers=True)
    assert summaries == []
    assert output == (("No other devices found to pull from.\n" if mode == "attended" else ""), "")


@pytest.mark.parametrize("mode", ("attended", "quiet"))
def test_dry_run_predicts_without_any_writes(fleet, monkeypatch, mode):
    _seed(fleet, {"note.md": b"peer content\n"})
    before = _snapshot(fleet.root)
    result, summaries, _ = _run(fleet, monkeypatch, mode, dry_run=True)
    _counts(result)
    assert summaries == [("", "")]
    assert _snapshot(fleet.root) == before


@pytest.mark.parametrize("mode", MODES)
def test_corrupt_blob_failure_lines_and_counts(fleet, monkeypatch, mode):
    peer, _ = _seed(fleet, {"note.md": b"peer content\n"})
    key = blob_key(PEER, peer["sources"]["notes"]["files"]["note.md"]["sha256"])
    raw = fleet.backend.get(key)
    fleet.backend.put(key, raw[:-1] + bytes([raw[-1] ^ 1]))
    result, summaries, output = _run(fleet, monkeypatch, mode, verbose=True)
    _counts(result, total_failed=1, device_names=["Peer B"])
    assert not (fleet.local / "note.md").exists()
    if mode == "quiet":
        assert summaries == [("", "mm: warning: Peer B/notes — 1 failed: note.md\n")]
        assert output == ("", "mm: warning: Peer B/notes — 1 failed: note.md\n")
    else:
        cause = (
            "decrypt failed: note.md — decrypt: GCM tag mismatch — wrong passphrase, "
            "wrong root_salt in mm-crypto-init, or corrupt blob. "
            "Verify mm-crypto-init integrity if other decrypts also fail."
        )
        assert cause not in " ".join(output[0].split())
        assert output[1] == f"mm: error: {cause}\n"
        summary = "  notes: 1 failed\n    - note.md\n"
        if mode == "direct":
            summary += (
                "\nPull incomplete: 0 written, 1 failed. Fix the warning above, then run "
                "'mm pull'; failed files are retried automatically on the next pull.\n"
                "  1 failed\n  Completed in <SECONDS>s\n"
            )
        assert summaries == [(summary, "")]


@pytest.mark.parametrize("mode", ("attended", "quiet"))
@pytest.mark.parametrize("dry_run", (False, True))
def test_fail_mode_refuses_apply_but_characterizes_migration_and_history(
    fleet, monkeypatch, mode, dry_run
):
    _seed(fleet, {"note.md": b"peer content\n", "excluded.md": b"excluded peer content\n"})
    fleet.config["sync"]["sources"][0]["exclude_patterns"] = ["excluded.md"]
    canonical = fleet.local / "note.md"
    canonical.write_bytes(b"local canonical\n")
    os.utime(canonical, (1_600_000_000, 1_600_000_000))
    legacy = fleet.local / "note.sync-conflict-20260101-100000-bbbbbbbb.md"
    legacy.write_bytes(b"legacy sidecar\n")
    before = _snapshot(fleet.root)
    refusal = {}
    with pytest.raises(typer.Exit) as stopped:
        _run(fleet, monkeypatch, mode, refusal=refusal, conflict_mode="fail", dry_run=dry_run)
    assert stopped.value.exit_code == 3
    warning = "mm: warning: conflict notes/note.md (from Peer B)\n"
    if dry_run:
        warning += "Nothing was changed except the local lock file (dry run).\n"
    assert refusal == {"summaries": [("", "")], "output": ("", warning)}
    assert canonical.read_bytes() == b"local canonical\n"
    assert not (fleet.local / "excluded.md").exists()
    if dry_run:
        assert _snapshot(fleet.root) == before
    else:
        # Fail preflight precedes incoming apply, but the existing migration
        # sweep occurs earlier. Attended pulls also record excluded paths;
        # quiet pulls skip that audit class. Neither ordinary pull is zero-write.
        assert not legacy.exists()
        migrated = fleet.local / "note.sync-conflict-v0-20260101-100000-bbbbbbbb.md"
        assert migrated.read_bytes() == b"legacy sidecar\n"
        assert (sidecar.SIDECAR_DIR / "inversion-installed-at").exists()
        assert statedb.db_path().exists() is (mode == "attended")
        expected_history = [("excluded", "excluded.md")] if mode == "attended" else []
        assert [
            (r["action"], r["rel_path"]) for r in pullhistory.read_records()
        ] == expected_history


def test_pullflow_contract_import_does_not_register_cli():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tests.test_pullflow; import sys; assert 'mind_meld.cli' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# ── Track 87A: reads that tolerate Syncthing ─────────────────────────

THIRD = "cccccccc"
CONFLICT_STAMP = "sync-conflict-20260101-101010-ABCDEFG"


@pytest.fixture
def syncthing(fleet, monkeypatch):
    """The fleet's storage as a Syncthing folder; `_pull_core` reads through it."""
    folder = SyncthingFolder(fleet.backend.root)
    monkeypatch.setattr(pullflow, "get_backend", lambda config: folder)
    return folder


def _blob(peer, name):
    return blob_key(PEER, peer["sources"]["notes"]["files"][name]["sha256"])


def _waiting(device_id, name, **fields):
    return {
        "device_id": device_id,
        "device_name": name,
        "files": 0,
        "manifest": False,
        "entry": False,
    } | fields


def _third_device(fleet, *, tombstones=None):
    """Register a third device whose manifest holds only `tombstones`."""
    devices.register_device(fleet.backend, THIRD, "Third C")
    devices.update_last_seen(fleet.backend, THIRD)
    third = {
        "device_id": THIRD,
        "device_name": "Third C",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sources": {},
        "tombstones": tombstones or {},
    }
    fleet.backend.put(
        manifest_key(THIRD),
        crypto.encrypt(manifest.serialize_manifest(third), PASSPHRASE, MEMORY_KB),
    )


@pytest.mark.parametrize("mode", MODES)
# Coverage audit value card:
# Value: protects=undelivered blobs create no file history before they arrive;
# fails_when=waiting is persisted as an apply event;
# why_new=existing waiting assertions cover counts and files but not history;
# seam=none; no_claim=live Syncthing delivery (temporary folder with withheld blobs).
def test_manifest_before_blobs_reports_waiting_then_applies(fleet, syncthing, monkeypatch, mode):
    peer, _ = _seed(fleet, {"a.md": b"alpha\n", "b.md": b"bravo\n"})
    keys = [_blob(peer, "a.md"), _blob(peer, "b.md")]
    for key in keys:
        syncthing.hide(key)

    result, summaries, output = _run(fleet, monkeypatch, mode, verbose=True)

    # Not delivered yet is neither a failure nor a degradation, and costs no retry.
    _counts(result, total_waiting=2, waiting=[_waiting(PEER, "Peer B", files=2)])
    assert result.total_failed == 0 and result.durability_fsync_failures == 0
    assert not (fleet.local / "a.md").exists() and not (fleet.local / "b.md").exists()
    assert syncthing.sleeps == []
    assert list(pullhistory.read_records()) == []
    if mode == "quiet":
        assert summaries == [("", "")]
    elif mode == "attended":
        assert summaries == [("  notes: 2 waiting\n    - a.md\n    - b.md\n", "")]
    else:
        text = summaries[0][0]
        assert "Pull complete." in text and "2 waiting" in text
        assert "waiting for 2 files from Peer B" in text
        assert "Pull incomplete" not in text

    for key in keys:
        syncthing.arrive(key)
    result, _, _ = _run(fleet, monkeypatch, mode)
    _counts(
        result,
        total_written=2,
        bytes_transferred=result.bytes_transferred,
        device_names=["Peer B"],
    )
    assert (fleet.local / "a.md").read_bytes() == b"alpha\n"
    assert (fleet.local / "b.md").read_bytes() == b"bravo\n"


def test_waiting_keeps_the_peer_manifest_copy_until_blobs_arrive(fleet, syncthing, monkeypatch):
    peer, _ = _seed(fleet, {"a.md": b"alpha\n", "b.md": b"bravo\n"})
    mkey = manifest_key(PEER)
    copy_key = mkey.replace("manifest.json.enc", f"manifest.json.{CONFLICT_STAMP}.enc")
    fleet.backend.put(copy_key, fleet.backend.get(mkey))
    syncthing.hide(_blob(peer, "b.md"))

    result, _, _ = _run(fleet, monkeypatch, "attended")

    # One file applied and one waiting: the copy may be the only holder of the
    # manifest the missing blob belongs to, so it stays.
    assert (result.total_written, result.total_waiting, result.total_failed) == (1, 1, 0)
    assert fleet.backend.exists(copy_key)

    syncthing.arrive(_blob(peer, "b.md"))
    result, _, _ = _run(fleet, monkeypatch, "attended")
    assert (result.total_written, result.total_waiting) == (1, 0)
    assert not fleet.backend.exists(copy_key)
    assert fleet.backend.exists(mkey)


def test_vanished_peer_manifest_is_waiting_not_corrupt(fleet, syncthing, monkeypatch):
    _seed(fleet, {"note.md": b"peer content\n"})
    syncthing.vanish_at_open(manifest_key(PEER))

    result, summaries, _ = _run(fleet, monkeypatch, "quiet")

    _counts(result, waiting=[_waiting(PEER, "Peer B", manifest=True)])
    assert result.corrupt_peer_count == 0 and result.total_failed == 0
    assert summaries == [("", "")]
    assert syncthing.sleeps == [0.05, 0.1, 0.2]


def test_vanished_own_manifest_is_data_at_risk_not_waiting(fleet, syncthing, monkeypatch):
    # The own manifest carries this Mac's deletion records; when it vanishes
    # mid-read they are not applied, so the pull must keep the corrupt-manifest
    # signal (stderr warning, degraded autopull), not report waiting.
    _seed(fleet, {"note.md": b"peer content\n"})
    own = manifest.build_manifest_v2(
        LOCAL,
        "Local A",
        [{"name": "notes", "type": "generic", "path": str(fleet.local), "include_dirs": ["."]}],
    )
    own["tombstones"] = {
        "notes:note.md": {
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "device_id": LOCAL,
        }
    }
    fleet.backend.put(
        manifest_key(LOCAL),
        crypto.encrypt(manifest.serialize_manifest(own), PASSPHRASE, MEMORY_KB),
    )
    syncthing.vanish_at_open(manifest_key(LOCAL))

    result, _, (_out, err) = _run(fleet, monkeypatch, "quiet")

    assert result.corrupt_peer_count == 1
    assert result.waiting == []
    assert (
        f"mm: warning: this device's manifest ({LOCAL}) vanished during the read - "
        "its deletion records were not applied" in err
    )
    assert "Check them before the next mm push." in err


def test_vanished_device_entry_is_waiting_and_never_refuses(fleet, syncthing, monkeypatch):
    _seed(fleet, {"note.md": b"peer content\n"})
    _third_device(fleet)
    syncthing.vanish_at_open(device_key(THIRD))

    result, _, _ = _run(fleet, monkeypatch, "attended")

    _counts(
        result,
        total_written=1,
        bytes_transferred=result.bytes_transferred,
        device_names=["Peer B"],
        waiting=[_waiting(THIRD, THIRD, entry=True)],
    )
    assert (fleet.local / "note.md").read_bytes() == b"peer content\n"
    # One inventory per pull feeds the gate, selection and the tombstone set.
    assert syncthing.listings.count("devices/") == 1


def test_vanished_device_still_supplies_its_tombstones(fleet, syncthing, monkeypatch):
    _seed(fleet, {"note.md": b"peer content\n"})
    _third_device(
        fleet,
        tombstones={
            "notes:note.md": {
                "deleted_at": datetime.now(timezone.utc).isoformat(),
                "device_id": THIRD,
            }
        },
    )
    syncthing.vanish_at_open(device_key(THIRD))

    result, _, _ = _run(fleet, monkeypatch, "attended")

    # Skipping the vanished device must not resurrect what it deleted.
    assert result.total_written == 0
    assert not (fleet.local / "note.md").exists()
    assert result.waiting[0].entry and result.waiting[0].device_id == THIRD


def test_pull_from_a_vanished_device_reports_waiting_not_unknown(fleet, syncthing, monkeypatch):
    _seed(fleet, {"note.md": b"peer content\n"})
    _third_device(fleet)
    syncthing.vanish_at_open(device_key(THIRD))

    result, _, _ = _run(fleet, monkeypatch, "attended", from_device=THIRD)

    _counts(result, waiting=[_waiting(THIRD, THIRD, entry=True)])
    assert not (fleet.local / "note.md").exists()


def test_no_readable_peers_left_still_names_the_vanished_one(fleet, syncthing, monkeypatch):
    fleet.backend.delete(device_key(PEER))
    _third_device(fleet)
    syncthing.vanish_at_open(device_key(THIRD))

    result, _, _ = _run(fleet, monkeypatch, "quiet")

    _counts(result, no_peers=True, waiting=[_waiting(THIRD, THIRD, entry=True)])


def test_conflict_copies_and_misnamed_device_files_never_refuse_or_duplicate(
    fleet, syncthing, monkeypatch
):
    _seed(fleet, {"note.md": b"peer content\n"})
    record = json.loads(fleet.backend.get(device_key(PEER)))
    stale = dict(record, last_seen_version="0.1.0")  # would trip the Track 5E gate
    for key in (
        f"devices/{PEER}.{CONFLICT_STAMP}.json",
        f"devices/{PEER} 2.json",
        "devices/renamed.json",
    ):
        fleet.backend.put(key, json.dumps(stale).encode())
    fleet.backend.put("devices/.syncthing.x.json.tmp", b"{")

    result, _, _ = _run(fleet, monkeypatch, "attended")

    _counts(
        result, total_written=1, bytes_transferred=result.bytes_transferred, device_names=["Peer B"]
    )
    assert result.waiting == []


# ── Track 91A: the pull rule, kept copies and propagated deletions ────
#
# Setup for every test below goes through the real ledger (`state.db` in the test's
# isolated state folder) and the real `_pull_core`; the recovery folder is
# `statedb.deleted_copies_dir()`, which the conftest points under tmp_path.

NOTES = "notes"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record(*rows, peer=PEER):
    """Commit `(path, data)` rows as pulled from `peer`: its base and a held version."""
    ledger = pullplan.Ledger()
    for path, data in rows:
        ledger.settle(peer, NOTES, path, _sha(data))
    assert core.commit_ledger(ledger) is None


def _committed(*peers):
    ledger, exc = core.load_ledger(peers or (PEER,))
    assert exc is None, exc
    return ledger


def _tombstone(data, *, device=PEER, age_days=0.0, **extra):
    """A peer's deletion record; `data` is the deleted bytes (None: a record with no hash)."""
    when = datetime.now(timezone.utc) - timedelta(days=age_days)
    record = {"deleted_at": when.isoformat(), "device_id": device}
    if data is not None:
        record["sha256"] = _sha(data)
    return record | extra


def _republish(fleet, peer_manifest):
    fleet.backend.put(
        manifest_key(PEER),
        crypto.encrypt(manifest.serialize_manifest(peer_manifest), PASSPHRASE, MEMORY_KB),
    )


def _shape_remote(fleet, peer_manifest, name, *, mtime=None, ancestors=None):
    """Give a seeded file a chosen manifest mtime and `from` list, then publish again."""
    info = peer_manifest["sources"][NOTES]["files"][name]
    if mtime is not None:
        info["mtime"] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    if ancestors is not None:
        info["from"] = [_sha(b)[:16] for b in ancestors]
    _republish(fleet, peer_manifest)


def _put_local(fleet, name, data, mtime):
    path = fleet.local / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def _copies():
    """Every file in the recovery folders: relative path under the dated folder -> bytes."""
    root = statedb.deleted_copies_dir()
    if not root.exists():
        return {}
    return {
        "/".join(p.relative_to(root).parts[1:]): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _older(*paths):
    return [{"device_name": "Peer B", "src_name": NOTES, "rel_path": p} for p in paths]


# ── with a recorded base, the characterization scenarios move to the new rule ──


@pytest.mark.parametrize("mode", MODES)
def test_newer_local_with_a_base_is_held_as_older_and_never_skipped_or_overwritten(
    fleet, monkeypatch, mode
):
    # The no-base `newer-local` scenario above pins the fallback (a skip). With a base
    # equal to the local file this machine did not edit, a peer edit with an older
    # timestamp is the restored-machine case: reported, not applied, nothing recorded.
    _seed(fleet, {"note.md": b"peer content\n"})
    local = _put_local(fleet, "note.md", b"local content\n", 1_800_000_000)
    _record(("note.md", b"local content\n"))

    result, summaries, output = _run(fleet, monkeypatch, mode, verbose=True)

    _counts(result, older=_older("note.md"))
    assert result.total_skipped == 0 and result.total_applied == 0
    assert local.read_bytes() == b"local content\n"
    assert list(fleet.local.glob("*.sync-conflict-*")) == []
    assert _committed().base(PEER, NOTES, "note.md") == _sha(b"local content\n")
    assert summaries[0][1] == ""  # nothing at risk is printed to stderr
    if mode != "quiet":
        assert "older peer edit not applied: Peer B/notes/note.md" in output[0]
    else:
        assert "older peer edit" not in output[0]


@pytest.mark.parametrize("mode", MODES)
def test_an_untouched_file_fast_forwards_instead_of_conflicting_when_a_base_exists(
    fleet, monkeypatch, mode
):
    # The no-base `conflict` scenario above keeps a conflict copy. With the base, the
    # local file is unedited: the peer's bytes simply land, with the peer's mtime.
    peer, transferred = _seed(fleet, {"note.md": b"peer content\n"})
    local = _put_local(fleet, "note.md", b"local content\n", 1_600_000_000)
    _record(("note.md", b"local content\n"))

    result, summaries, _ = _run(fleet, monkeypatch, mode)

    _counts(result, total_written=1, bytes_transferred=transferred, device_names=["Peer B"])
    assert local.read_bytes() == b"peer content\n"
    assert int(local.stat().st_mtime) == 1_700_000_000  # the peer's mtime is restored
    assert list(fleet.local.glob("*.sync-conflict-*")) == []
    assert summaries[0][1] == ""
    ledger = _committed()
    assert ledger.base(PEER, NOTES, "note.md") == _sha(b"peer content\n")
    assert ledger.newest_held(NOTES, "note.md") == _sha(b"peer content\n")[:16]


@pytest.mark.parametrize("mode", MODES)
def test_a_mergeable_file_the_owner_did_not_touch_fast_forwards_with_no_union(
    fleet, monkeypatch, mode
):
    # A deleted line stays deleted: the remote is applied whole, never unioned.
    old, new = b'{"keep":1}\n{"gone":2}\n', b'{"keep":1}\n'
    _seed(fleet, {"events.jsonl": new})
    local = _put_local(fleet, "events.jsonl", old, 1_600_000_000)
    _record(("events.jsonl", old))

    result, _, _ = _run(fleet, monkeypatch, mode)

    assert (result.total_written, result.total_merged, result.total_conflicted) == (1, 0, 0)
    assert local.read_bytes() == new


@pytest.mark.parametrize("mode", MODES)
def test_a_mergeable_file_both_sides_changed_still_unions_with_a_base(fleet, monkeypatch, mode):
    _seed(fleet, {"events.jsonl": b'{"remote":1}\n'})
    local = _put_local(fleet, "events.jsonl", b'{"local":1}\n', 1_600_000_000)
    _record(("events.jsonl", b'{"base":1}\n'))

    result, _, _ = _run(fleet, monkeypatch, mode)

    assert (result.total_written, result.total_merged, result.total_conflicted) == (0, 1, 0)
    assert local.read_bytes() == b'{"local":1}\n{"remote":1}\n'
    ledger = _committed()
    assert ledger.base(PEER, NOTES, "events.jsonl") == _sha(b'{"remote":1}\n')
    held = {p for p, e in ledger.entries(NOTES, "events.jsonl").items() if e.kind == "held"}
    assert {_sha(b'{"local":1}\n')[:16], _sha(b'{"remote":1}\n')[:16]} <= held


@pytest.mark.parametrize("mode", MODES)
def test_both_sides_changed_a_plain_file_conflicts_even_when_local_is_newer(
    fleet, monkeypatch, mode
):
    # With a base the local-newer skip is gone: the peer holding the older edit is told.
    _seed(fleet, {"note.md": b"peer content\n"})
    local = _put_local(fleet, "note.md", b"local edit\n", 1_800_000_000)
    _record(("note.md", b"the shared version\n"))

    result, _, _ = _run(fleet, monkeypatch, mode)

    assert (result.total_conflicted, result.total_skipped, result.total_written) == (1, 0, 0)
    assert local.read_bytes() == b"local edit\n"
    (copy,) = fleet.local.glob("note.sync-conflict-*.md")
    assert copy.read_bytes() == b"peer content\n"


@pytest.mark.parametrize("mode", MODES)
def test_a_hashed_tombstone_with_a_matching_base_deletes_into_the_recovery_folder(
    fleet, monkeypatch, mode
):
    # The no-base `tombstone` scenario above pins today's suppression. With the base and
    # the record's hash both equal to the local bytes, the file is deleted and recoverable.
    content = b"shared content\n"
    _seed(fleet, {}, tombstones={"notes:note.md": _tombstone(content)})
    local = _put_local(fleet, "note.md", content, 1_600_000_000)
    _record(("note.md", content))

    result, summaries, output = _run(fleet, monkeypatch, mode)

    assert not local.exists()
    assert result.total_deleted == 1 and result.total_failed == 0
    assert result.deleted_origins == ["Peer B"]
    assert result.total_applied == 0
    assert _copies() == {"notes/note.md": content}
    folder = Path(result.deleted_folder)
    assert folder.parent == statedb.deleted_copies_dir() and folder.is_dir()
    assert summaries[0][1] == ""  # the outcome line is the CLI's; stderr stays clean


def test_the_direct_summary_of_a_deletion_says_what_was_deleted(fleet, monkeypatch):
    content = b"shared content\n"
    _seed(fleet, {}, tombstones={"notes:note.md": _tombstone(content)})
    _put_local(fleet, "note.md", content, 1_600_000_000)
    _record(("note.md", content))

    result, summaries, _ = _run(fleet, monkeypatch, "direct")

    assert result.total_deleted == 1
    assert "nothing to apply" not in summaries[0][0]
    assert "1 deleted" in summaries[0][0]


# ── a kept conflict copy is not a new outcome (criterion 10) ──────────

KEPT_SIDECAR = "note.sync-conflict-20260421-120000-v1-bbbbbbbb.md"


def _seed_kept_conflict(fleet):
    """The case observed on a real machine: a copy already holds the peer's bytes, no base."""
    _seed(fleet, {"note.md": b"peer content\n"})
    local = _put_local(fleet, "note.md", b"local content\n", 1_600_000_000)
    copy = fleet.local / KEPT_SIDECAR
    copy.write_bytes(b"peer content\n")
    return local, copy


@pytest.mark.parametrize("mode", MODES)
def test_a_conflict_copy_already_holding_the_peers_bytes_counts_nothing_and_warns_nothing(
    fleet, monkeypatch, mode
):
    local, copy = _seed_kept_conflict(fleet)
    assert _committed().base(PEER, NOTES, "note.md") is None

    result, summaries, (out, err) = _run(fleet, monkeypatch, mode)

    _counts(result)  # no applied file, no conflict, no device name, nothing waiting
    assert result.total_applied == 0 and result.total_conflicted == 0
    assert err == ""
    stdout = (
        "\nPull complete.\n  nothing to apply\n  Completed in <SECONDS>s\n"
        if mode == "direct"
        else ""
    )
    assert summaries == [(stdout, "")]
    assert "conflict" not in out.lower().replace("sync-conflict", "")
    # Nothing was written: both files are exactly as left, and the copy stays listed.
    assert local.read_bytes() == b"local content\n" and copy.read_bytes() == b"peer content\n"
    assert [(n, p) for n, p, _ in resolveflow._find_conflict_files(fleet.config)] == [(NOTES, copy)]
    assert pullhistory.read_records() == []  # no history row for a non-event


def test_a_kept_copy_settles_the_peers_version_so_the_next_pull_is_silent_too(fleet, monkeypatch):
    _seed_kept_conflict(fleet)
    _run(fleet, monkeypatch, "quiet")
    ledger = _committed()
    assert ledger.base(PEER, NOTES, "note.md") == _sha(b"peer content\n")
    # A conflict copy holds the version without this machine having built on it.
    assert ledger.newest_held(NOTES, "note.md") is None
    assert ledger.entries(NOTES, "note.md")[_sha(b"peer content\n")[:16]].kind == "seen"
    result, summaries, (_, err) = _run(fleet, monkeypatch, "attended")
    _counts(result)
    assert (summaries, err) == ([("", "")], "")


def test_a_lost_base_brings_back_the_same_single_quiet_outcome(fleet, monkeypatch):
    local, copy = _seed_kept_conflict(fleet)
    _run(fleet, monkeypatch, "quiet")
    statedb.db_path().unlink()  # README's move-aside remedy: the ledger is rebuilt from nothing
    result, summaries, (_, err) = _run(fleet, monkeypatch, "attended")
    _counts(result)
    assert (summaries, err) == ([("", "")], "")
    assert len(list(fleet.local.glob("*.sync-conflict-*"))) == 1
    assert copy.read_bytes() == b"peer content\n" and local.read_bytes() == b"local content\n"
    assert _committed().base(PEER, NOTES, "note.md") == _sha(b"peer content\n")


def test_a_newer_peer_version_replaces_the_kept_copy_once_and_counts_as_a_conflict(
    fleet, monkeypatch
):
    # "kept" is only for bytes the copy already holds: a different peer version is news.
    local, copy = _seed_kept_conflict(fleet)
    copy.write_bytes(b"an older peer version\n")
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert result.total_conflicted == 1
    (only,) = fleet.local.glob("*.sync-conflict-*")
    assert only.read_bytes() == b"peer content\n"
    assert local.read_bytes() == b"local content\n"


# ── a conflict that exists when this ships clears with `mm resolve` (criterion 11) ──

MULTILINE_LOCAL = b"one\ntwo\nthree\nlocal edit\n"
MULTILINE_PEER = b"one\ntwo\nthree\npeer edit\n"


@pytest.mark.parametrize("choice", ["l", "r", "m"])
def test_after_resolving_an_existing_conflict_a_later_pull_writes_and_conflicts_nothing(
    fleet, monkeypatch, choice
):
    import typer

    _seed(fleet, {"note.md": MULTILINE_PEER})
    local = _put_local(fleet, "note.md", MULTILINE_LOCAL, 1_600_000_000)
    copy = fleet.local / KEPT_SIDECAR
    copy.write_bytes(MULTILINE_PEER)
    os.utime(copy, (1_700_000_000, 1_700_000_000))
    assert not statedb.db_path().exists()  # no base: the conflict predates the ledger

    hits = resolveflow._find_conflict_files(fleet.config)
    assert [(n, p) for n, p, _ in hits] == [(NOTES, copy)]
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: choice)
    assert resolveflow._resolve_interactive_loop(hits) == (1, 0)
    assert not copy.exists()
    resolved_bytes = local.read_bytes()
    assert resolved_bytes == {"l": MULTILINE_LOCAL, "r": MULTILINE_PEER}.get(choice, resolved_bytes)

    result, summaries, (_, err) = _run(fleet, monkeypatch, "attended")

    assert (result.total_written, result.total_merged, result.total_conflicted) == (0, 0, 0)
    assert result.total_failed == 0 and result.older == []
    assert local.read_bytes() == resolved_bytes  # untouched
    assert list(fleet.local.glob("*.sync-conflict-*")) == []  # and no new copy
    assert resolveflow._find_conflict_files(fleet.config) == []
    assert (summaries, err) == ([("", "")], "")
    # The next pull is just as quiet.
    again, _, _ = _run(fleet, monkeypatch, "quiet")
    assert (again.total_written, again.total_merged, again.total_conflicted) == (0, 0, 0)
    assert local.read_bytes() == resolved_bytes


def test_a_conflict_with_no_base_does_not_clear_by_itself(fleet, monkeypatch):
    # No base means today's rule: a differing sidecar is replaced, never removed.
    _seed(fleet, {"note.md": MULTILINE_PEER})
    local = _put_local(fleet, "note.md", MULTILINE_LOCAL, 1_600_000_000)
    for _ in range(2):
        _run(fleet, monkeypatch, "quiet")
        assert local.read_bytes() == MULTILINE_LOCAL
        assert len(list(fleet.local.glob("note.sync-conflict-*.md"))) == 1
        assert len(resolveflow._find_conflict_files(fleet.config)) == 1


# ── propagated deletions: when a peer's record deletes a local file ────

SHARED = b"shared content\n"


def _candidates(fleet, tombstones, *, files=None, sources=None, **overrides):
    """What `_deletion_candidates` returns for one peer's manifest, plus its `keep` reasons."""
    remote = {"sources": {NOTES: {"files": files or {}}}, "tombstones": tombstones}
    keeps: list = []
    found = pullflow._deletion_candidates(
        pull_targets=[{"device_id": PEER, "device_name": "Peer B"}],
        manifest_cache={PEER: remote},
        local_sources_map=sources or {NOTES: {"path": fleet.local, "type": "generic"}},
        source_filter=overrides.get("source_filter"),
        ledger=overrides.get("ledger") or _committed(),
        my_device_id=LOCAL,
        blocked=overrides.get("blocked"),
        keeps=keeps,
    )
    return found, [reason for _, _, _, reason in keeps]


def _deletable(fleet, name="note.md", data=SHARED):
    """A local file that a hashed record, a matching base and a matching peer manifest delete."""
    path = _put_local(fleet, name, data, 1_600_000_000)
    _record((name, data))
    return path, {f"notes:{name}": _tombstone(data)}


def test_a_file_is_a_candidate_only_when_every_guard_holds(fleet):
    path, tombs = _deletable(fleet)
    found, keeps = _candidates(fleet, tombs)
    assert [(d.peer_id, d.src_name, d.rel_path, d.sha) for d in found] == [
        (PEER, NOTES, "note.md", _sha(SHARED))
    ]
    assert keeps == [] and path.exists()


def test_a_record_with_no_hash_never_deletes_it_only_suppresses(fleet):
    # Every record from a release before this one, and every old-form record.
    _, tombs = _deletable(fleet)
    tombs["notes:note.md"].pop("sha256")
    found, keeps = _candidates(fleet, tombs)
    assert found == []
    assert any("no content hash" in k for k in keeps)


@pytest.mark.parametrize("bad_hash", ["abc", "A" * 64, "g" * 64, 7, None, ["a" * 64]])
def test_a_malformed_hash_reads_as_no_hash(fleet, bad_hash):
    _, tombs = _deletable(fleet)
    tombs["notes:note.md"]["sha256"] = bad_hash
    found, _ = _candidates(fleet, tombs)
    assert found == []


def test_an_expired_record_deletes_nothing(fleet):
    path, _ = _deletable(fleet)
    old = {"notes:note.md": _tombstone(SHARED, age_days=manifest.TOMBSTONE_TTL_DAYS + 1)}
    found, _ = _candidates(fleet, old)
    assert found == [] and path.exists()


def test_a_hash_that_differs_from_the_recorded_base_deletes_nothing(fleet):
    path, tombs = _deletable(fleet)
    _record(("note.md", b"another version\n"))  # base now names a different version
    found, keeps = _candidates(fleet, tombs)
    assert found == [] and path.exists()
    assert any("never synced that version" in k for k in keeps)


def test_a_file_with_no_recorded_base_is_left_alone(fleet):
    path = _put_local(fleet, "note.md", SHARED, 1_600_000_000)
    found, keeps = _candidates(fleet, {"notes:note.md": _tombstone(SHARED)})
    assert found == [] and path.exists()
    assert any("never synced that version" in k for k in keeps)


def test_local_bytes_that_differ_from_the_deleted_version_survive(fleet):
    path, tombs = _deletable(fleet)
    path.write_bytes(b"edited since\n")
    found, keeps = _candidates(fleet, tombs)
    assert found == [] and path.read_bytes() == b"edited since\n"
    assert any("bytes differ" in k for k in keeps)


def test_a_peer_that_still_lists_the_path_deletes_nothing(fleet):
    _, tombs = _deletable(fleet)
    found, keeps = _candidates(fleet, tombs, files={"note.md": {"sha256": _sha(b"new\n")}})
    assert found == []
    assert any("lists it again" in k for k in keeps)


def test_a_record_naming_another_machine_deletes_nothing(fleet):
    # A recovery can carry a peer's copy of someone else's record; its origin may have restored.
    path, _ = _deletable(fleet)
    found, keeps = _candidates(fleet, {"notes:note.md": _tombstone(SHARED, device=THIRD)})
    assert found == [] and path.exists()
    assert any("another machine" in k for k in keeps)


def test_an_unknown_or_filtered_source_deletes_nothing(fleet):
    path, tombs = _deletable(fleet)
    other = {"other": {"path": fleet.local, "type": "generic"}}
    assert _candidates(fleet, tombs, sources=other)[0] == []  # disabled / not configured
    assert _candidates(fleet, tombs, source_filter="other")[0] == []  # --source filter
    assert len(_candidates(fleet, tombs, source_filter=NOTES)[0]) == 1
    assert path.exists()


def test_mm_events_records_never_delete(fleet):
    events = fleet.local / "2026-01-01.jsonl"
    events.write_bytes(SHARED)
    ledger = pullplan.Ledger()
    ledger.settle(PEER, "mm-events", "2026-01-01.jsonl", _sha(SHARED))
    assert core.commit_ledger(ledger) is None
    sources = {"mm-events": {"path": fleet.local, "type": "generic"}}
    remote = {
        "sources": {"mm-events": {"files": {}}},
        "tombstones": {"mm-events:2026-01-01.jsonl": _tombstone(SHARED)},
    }
    found = pullflow._deletion_candidates(
        pull_targets=[{"device_id": PEER, "device_name": "Peer B"}],
        manifest_cache={PEER: remote},
        local_sources_map=sources,
        source_filter=None,
        ledger=_committed(),
        my_device_id=LOCAL,
    )
    assert found == [] and events.read_bytes() == SHARED


def test_this_machines_own_record_never_deletes_here(fleet):
    path, tombs = _deletable(fleet)
    found = pullflow._deletion_candidates(
        pull_targets=[{"device_id": LOCAL, "device_name": "Local A"}],
        manifest_cache={LOCAL: {"sources": {NOTES: {"files": {}}}, "tombstones": tombs}},
        local_sources_map={NOTES: {"path": fleet.local, "type": "generic"}},
        source_filter=None,
        ledger=_committed(),
        my_device_id=LOCAL,
    )
    assert found == [] and path.exists()


def test_a_symlinked_folder_on_the_way_survives(fleet, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "note.md").write_bytes(SHARED)
    (fleet.local / "sub").symlink_to(elsewhere)
    _record(("sub/note.md", SHARED))
    found, keeps = _candidates(fleet, {"notes:sub/note.md": _tombstone(SHARED)})
    assert found == [] and (elsewhere / "note.md").read_bytes() == SHARED
    assert any("symlink" in k for k in keeps)


def test_a_symlink_leaf_survives_and_its_target_is_untouched(fleet, tmp_path):
    target = tmp_path / "target.md"
    target.write_bytes(SHARED)
    (fleet.local / "note.md").symlink_to(target)
    _record(("note.md", SHARED))
    found, _ = _candidates(fleet, {"notes:note.md": _tombstone(SHARED)})
    assert found == [] and target.read_bytes() == SHARED and (fleet.local / "note.md").is_symlink()


def test_a_path_that_is_not_a_regular_file_survives(fleet):
    (fleet.local / "note.md").mkdir()
    _record(("note.md", SHARED))
    found, keeps = _candidates(fleet, {"notes:note.md": _tombstone(SHARED)})
    assert found == [] and (fleet.local / "note.md").is_dir()
    assert any("not a regular file" in k for k in keeps)


def test_a_case_only_rename_on_a_case_insensitive_volume_survives(fleet, monkeypatch):
    # A Linux peer renamed Note.md to note.md: its record names the old spelling while it
    # lists the new one. On a case-insensitive volume both spellings are this one file.
    path, tombs = _deletable(fleet, name="note.md")
    monkeypatch.setattr(pullflow, "_detect_case_insensitive_fs", lambda p: True)
    found, keeps = _candidates(fleet, tombs, files={"NOTE.md": {"sha256": _sha(SHARED)}})
    assert found == [] and path.exists()
    assert any("differs only in case" in k for k in keeps)
    # On a case-sensitive volume the two names are different files: the deletion stands.
    monkeypatch.setattr(pullflow, "_detect_case_insensitive_fs", lambda p: False)
    found, _ = _candidates(fleet, tombs, files={"NOTE.md": {"sha256": _sha(SHARED)}})
    assert len(found) == 1


@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "apply"])
def test_a_case_only_replacement_survives_collision_filtering(fleet, monkeypatch, dry_run):
    """The local publication wins collision selection, but cannot authorize deletion."""
    local = _put_local(fleet, "README.md", SHARED, 1_600_000_000)
    own = manifest.build_manifest_v2(LOCAL, "Local A", fleet.config["sync"]["sources"])
    fleet.backend.put(
        manifest_key(LOCAL),
        crypto.encrypt(manifest.serialize_manifest(own), PASSPHRASE, MEMORY_KB),
    )
    _record(("README.md", SHARED))
    _seed(fleet, {"readme.md": SHARED}, tombstones={"notes:README.md": _tombstone(SHARED)})
    monkeypatch.setattr(pullflow, "_detect_case_insensitive_fs", lambda path: True)

    result, _, output = _run(fleet, monkeypatch, "attended", dry_run=dry_run, verbose=True)

    assert "case-collision" in output[1]  # The replacement really was filtered out.
    assert local.exists(), "a Linux case-only rename must not delete the Mac's live file"
    assert local.read_bytes() == SHARED and result.total_deleted == 0
    assert _copies() == {}
    assert _committed().base(PEER, NOTES, "README.md") == _sha(SHARED)
    if dry_run:
        assert "differs only in case" in output[0]


def test_a_waiting_or_failed_peer_source_blocks_its_deletions(fleet):
    _, tombs = _deletable(fleet)
    assert _candidates(fleet, tombs, blocked={(PEER, NOTES)})[0] == []
    assert len(_candidates(fleet, tombs, blocked={(PEER, "elsewhere")})[0]) == 1


def test_an_unreadable_local_file_survives(fleet):
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    path, tombs = _deletable(fleet)
    path.chmod(0)
    try:
        found, keeps = _candidates(fleet, tombs)
    finally:
        path.chmod(0o600)
    assert found == [] and path.exists()
    assert any("could not be read" in k for k in keeps)


def test_an_unreadable_ledger_deletes_nothing(fleet):
    _, tombs = _deletable(fleet)
    dead = pullplan.Ledger(readable=False)
    assert _candidates(fleet, tombs, ledger=dead)[0] == []


def test_two_peers_naming_one_file_give_one_candidate(fleet):
    _, tombs = _deletable(fleet)
    ledger = _committed()
    ledger.settle(THIRD, NOTES, "note.md", _sha(SHARED))

    def remote(device):
        return {
            "sources": {NOTES: {"files": {}}},
            "tombstones": {"notes:note.md": _tombstone(SHARED, device=device)},
        }

    found = pullflow._deletion_candidates(
        pull_targets=[
            {"device_id": PEER, "device_name": "Peer B"},
            {"device_id": THIRD, "device_name": "Third C"},
        ],
        manifest_cache={PEER: remote(PEER), THIRD: remote(THIRD)},
        local_sources_map={NOTES: {"path": fleet.local, "type": "generic"}},
        source_filter=None,
        ledger=ledger,
        my_device_id=LOCAL,
    )
    assert len(found) == 1 and tombs


# ── a pull that deletes: the recovery copy, the history row, the ledger ──


def _setup_deletion(fleet, name="note.md", data=SHARED):
    path = _put_local(fleet, name, data, 1_600_000_000)
    _record((name, data))
    _seed(fleet, {}, tombstones={f"notes:{name}": _tombstone(data)})
    return path


@pytest.mark.parametrize("mode", MODES)
def test_a_followed_deletion_keeps_a_private_recovery_copy_and_says_so_in_the_result(
    fleet, monkeypatch, mode
):
    path = _setup_deletion(fleet, "sub/dir/note.md")

    result, _, (_, err) = _run(fleet, monkeypatch, mode)

    assert not path.exists()
    assert (result.total_deleted, result.total_failed, result.total_applied) == (1, 0, 0)
    assert result.deleted_origins == ["Peer B"]
    folder = Path(result.deleted_folder)
    assert folder.parent == statedb.deleted_copies_dir()
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", folder.name)
    assert _copies() == {"notes/sub/dir/note.md": SHARED}
    # Private: every folder 0700 and the copy 0600, like the rest of the state folder.
    for directory in (
        folder,
        folder / NOTES,
        folder / NOTES / "sub",
        folder / NOTES / "sub" / "dir",
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    assert stat.S_IMODE((folder / NOTES / "sub" / "dir" / "note.md").stat().st_mode) == 0o600
    assert err == ""


def test_a_followed_deletion_is_recorded_in_the_history_with_its_folder(fleet, monkeypatch):
    _setup_deletion(fleet)
    result, _, _ = _run(fleet, monkeypatch, "attended")
    rows = [r for r in pullhistory.read_records() if r["action"] == "deleted"]
    assert len(rows) == 1
    assert (rows[0]["verb"], rows[0]["source"], rows[0]["rel_path"]) == ("pull", NOTES, "note.md")
    assert rows[0]["folder"] == result.deleted_folder
    assert rows[0]["device"] == PEER
    assert pullhistory.query(action="deleted")  # `mm log --action deleted` finds it


def test_a_followed_deletion_clears_the_bases_and_stamps_the_deleted_version(fleet, monkeypatch):
    _setup_deletion(fleet)
    ledger = pullplan.Ledger()
    ledger.settle(THIRD, NOTES, "note.md", _sha(SHARED))  # another peer's base is cleared too
    assert core.commit_ledger(ledger) is None
    _run(fleet, monkeypatch, "quiet")
    after = _committed(PEER, THIRD)
    assert after.base(PEER, NOTES, "note.md") is None
    assert after.base(THIRD, NOTES, "note.md") is None
    row = after.entries(NOTES, "note.md")[_sha(SHARED)[:16]]
    assert (row.kind, row.deleted_by) == ("held", PEER)
    assert after.newest_stamp(NOTES, "note.md") == PEER


def test_after_a_followed_deletion_the_next_pull_neither_restores_nor_deletes_again(
    fleet, monkeypatch
):
    path = _setup_deletion(fleet)
    _run(fleet, monkeypatch, "quiet")
    again, summaries, (_, err) = _run(fleet, monkeypatch, "attended")
    _counts(again)
    assert not path.exists() and (summaries, err) == ([("", "")], "")
    assert len(_copies()) == 1  # one copy, from the first pull only


def test_the_source_filter_narrows_deletions(fleet, monkeypatch):
    path = _setup_deletion(fleet)
    result, _, _ = _run(fleet, monkeypatch, "quiet", source_filter="elsewhere")
    assert result.total_deleted == 0 and path.exists() and _copies() == {}
    result, _, _ = _run(fleet, monkeypatch, "quiet", source_filter=NOTES)
    assert result.total_deleted == 1 and not path.exists()


def test_one_record_from_two_peers_gives_one_copy_and_one_count(fleet, monkeypatch):
    path = _setup_deletion(fleet)
    _third_device(fleet, tombstones={"notes:note.md": _tombstone(SHARED, device=THIRD)})
    ledger = pullplan.Ledger()
    ledger.settle(THIRD, NOTES, "note.md", _sha(SHARED))
    assert core.commit_ledger(ledger) is None

    result, _, _ = _run(fleet, monkeypatch, "quiet")

    assert not path.exists() and result.total_deleted == 1
    assert len(_copies()) == 1
    assert len([r for r in pullhistory.read_records() if r["action"] == "deleted"]) == 1


def test_an_older_peers_hashless_record_still_suppresses_but_deletes_nothing(fleet, monkeypatch):
    # The choice for criteria 9 and 20: a record from a release before this one has no hash,
    # so it keeps today's effect (it stops the peer's copy being downloaded) and never deletes.
    content = SHARED
    _seed(fleet, {"note.md": b"peer has it too\n"}, tombstones={"notes:note.md": _tombstone(None)})
    path = _put_local(fleet, "note.md", content, 1_600_000_000)
    _record(("note.md", content))
    result, _, _ = _run(fleet, monkeypatch, "attended")
    assert path.read_bytes() == content  # not deleted, and the peer's bytes were not downloaded
    assert (result.total_deleted, result.total_written, result.total_conflicted) == (0, 0, 0)
    assert _copies() == {}
    # Without the local file the record still stops the re-download.
    path.unlink()
    result, _, _ = _run(fleet, monkeypatch, "attended")
    assert result.total_written == 0 and not path.exists()


def test_a_same_named_file_with_different_bytes_survives_a_record(fleet, monkeypatch):
    # The path was re-cloned from another repository: same name, different bytes.
    _record(("note.md", SHARED))
    path = _put_local(fleet, "note.md", b"another repository's file\n", 1_600_000_000)
    _seed(fleet, {}, tombstones={"notes:note.md": _tombstone(SHARED)})
    result, _, _ = _run(fleet, monkeypatch, "attended")
    assert path.read_bytes() == b"another repository's file\n"
    assert result.total_deleted == 0 and _copies() == {}


@pytest.mark.parametrize(
    "name", [".mind-meld-anchor", "node_modules/pkg/index.js", "dir/.mind-meld-anchor"]
)
def test_excluded_paths_are_never_deleted_by_a_record(fleet, monkeypatch, name):
    fleet.config["sync"]["sources"][0]["exclude_patterns"] = ["node_modules/*"]
    path = _setup_deletion(fleet, name)
    result, _, _ = _run(fleet, monkeypatch, "attended")
    assert path.read_bytes() == SHARED
    assert result.total_deleted == 0 and _copies() == {}


def test_a_disabled_source_is_never_deleted_from(fleet, monkeypatch):
    path = _setup_deletion(fleet)
    fleet.config["sync"]["disabled_sources"] = [NOTES]
    result, _, _ = _run(fleet, monkeypatch, "attended")
    assert path.read_bytes() == SHARED and result.total_deleted == 0 and _copies() == {}


def test_a_peer_with_undelivered_blobs_blocks_its_deletions_this_pull(
    fleet, syncthing, monkeypatch
):
    # A rename's replacement can still be on its way: nothing is deleted until the peer is whole.
    path = _put_local(fleet, "note.md", SHARED, 1_600_000_000)
    _record(("note.md", SHARED))
    peer, _ = _seed(
        fleet, {"new.md": b"replacement\n"}, tombstones={"notes:note.md": _tombstone(SHARED)}
    )
    syncthing.hide(_blob(peer, "new.md"))

    result, _, _ = _run(fleet, monkeypatch, "attended")

    assert result.total_waiting == 1 and result.total_deleted == 0
    assert path.read_bytes() == SHARED and _copies() == {}
    syncthing.arrive(_blob(peer, "new.md"))
    result, _, _ = _run(fleet, monkeypatch, "attended")
    assert result.total_written == 1 and result.total_deleted == 1 and not path.exists()


def test_a_failing_ledger_commit_deletes_nothing_and_warns(fleet, monkeypatch):
    path = _setup_deletion(fleet)
    boom = RuntimeError("disk full")
    with monkeypatch.context() as broken:
        broken.setattr(core, "commit_ledger", lambda ledger: boom)
        result, _, (_, err) = _run(fleet, broken, "quiet")

    assert path.read_bytes() == SHARED  # nothing deleted without the record that explains it
    assert result.total_deleted == 0 and result.total_failed == 1
    assert result.ledger_cause == "unreadable"
    assert "deletion not recorded" in err and "disk full" in err
    assert _copies() == {}  # no stray copy of a file that was not deleted
    # The base is still there, so the next pull deletes it once the ledger works again.
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert result.total_deleted == 1 and not path.exists()


def test_a_failed_ledger_commit_leaves_no_unused_recovery_folder(fleet, monkeypatch):
    _setup_deletion(fleet)
    with monkeypatch.context() as broken:
        broken.setattr(core, "commit_ledger", lambda ledger: RuntimeError("disk full"))
        _run(fleet, broken, "quiet")
    root = statedb.deleted_copies_dir()
    assert not root.exists() or list(root.iterdir()) == []


def test_a_recovery_folder_that_cannot_be_made_deletes_nothing(fleet, monkeypatch):
    path = _setup_deletion(fleet)

    def refuse(folder, deletions):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(pullflow, "_make_copy_dirs", refuse)
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    assert path.read_bytes() == SHARED
    assert (result.total_deleted, result.total_failed) == (0, 1)
    assert "cannot create recovery folder" in err
    assert _committed().base(PEER, NOTES, "note.md") == _sha(SHARED)  # the base stays


def _on(real, path, then):
    """Wrap `os.rename` so `then(real, src, dst)` runs only for `path`."""

    def wrapper(src, dst, *args, **kwargs):
        if Path(src) == path:
            return then(real, src, dst, *args, **kwargs)
        return real(src, dst, *args, **kwargs)

    return wrapper


def test_a_rename_that_fails_for_lack_of_space_keeps_the_file_and_counts_failed(fleet, monkeypatch):
    path = _setup_deletion(fleet)

    def full(real, src, dst, *args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "rename", _on(os.rename, path, full))
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    assert path.read_bytes() == SHARED
    assert (result.total_deleted, result.total_failed) == (0, 1)
    assert "delete failed" in err and "free disk space" in err
    assert _copies() == {}


def test_a_write_before_the_move_puts_the_edited_file_back(fleet, monkeypatch):
    path = _setup_deletion(fleet)
    path.chmod(0o755)

    def edit_then_move(real, src, dst, *args, **kwargs):
        path.write_bytes(b"typed after the check\n")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", _on(os.rename, path, edit_then_move))
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    # The moved inode held the edit, not the deleted version: it is linked back.
    assert path.read_bytes() == b"typed after the check\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert (result.total_deleted, result.total_failed) == (0, 1)
    assert "changed while it was being deleted" in err and "local file kept" in err
    assert _copies() == {}


def _ledger_state(rel):
    ledger, exc = core.load_ledger([PEER])
    assert exc is None
    return ledger.base(PEER, NOTES, rel), ledger.newest_stamp(NOTES, rel)


def test_a_deletion_that_did_not_happen_is_not_remembered_as_followed(fleet, monkeypatch):
    # The ledger rows (base dropped, version stamped) are committed before the move. When
    # the move then puts the edited file back, they are taken back: otherwise a later hand
    # deletion would read as merely followed and never propagate.
    path = _setup_deletion(fleet)

    def edit_then_move(real, src, dst, *args, **kwargs):
        path.write_bytes(b"typed after the check\n")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", _on(os.rename, path, edit_then_move))
    _run(fleet, monkeypatch, "quiet")
    assert path.exists()
    # The edited bytes are not the peer's version, so the old base is not claimed for them,
    # but nothing marks the path as a followed deletion.
    assert _ledger_state("note.md")[1] is None


def test_a_failed_ledger_commit_leaves_no_followed_deletion_behind(fleet, monkeypatch):
    path = _setup_deletion(fleet)
    real, calls = core.commit_ledger, []

    def flaky(ledger):
        calls.append(1)
        return RuntimeError("busy") if len(calls) == 1 else real(ledger)

    with monkeypatch.context() as m:
        m.setattr(core, "commit_ledger", flaky)
        result, _, _ = _run(fleet, m, "quiet")
    assert path.exists() and result.total_deleted == 0
    base, stamp = _ledger_state("note.md")
    assert base == hashlib.sha256(SHARED).hexdigest() and stamp is None


def test_a_path_re_created_after_the_move_keeps_both_files(fleet, monkeypatch):
    path = _setup_deletion(fleet)

    def edit_move_recreate(real, src, dst, *args, **kwargs):
        path.write_bytes(b"typed after the check\n")
        done = real(src, dst, *args, **kwargs)
        path.write_bytes(b"a brand new file\n")
        return done

    monkeypatch.setattr(os, "rename", _on(os.rename, path, edit_move_recreate))
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    assert path.read_bytes() == b"a brand new file\n"  # never clobbered
    assert _copies() == {"notes/note.md": b"typed after the check\n"}
    assert (result.total_deleted, result.total_failed) == (0, 1)
    assert "moved file kept in recovery folder" in err


def test_a_writer_replacing_the_path_after_the_move_keeps_its_file(fleet, monkeypatch):
    # Moving the actual inode closes the check-then-unlink window: the new file survives.
    path = _setup_deletion(fleet)

    def move_then_write(real, src, dst, *args, **kwargs):
        done = real(src, dst, *args, **kwargs)
        path.write_bytes(b"written right after\n")
        return done

    monkeypatch.setattr(os, "rename", _on(os.rename, path, move_then_write))
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert path.read_bytes() == b"written right after\n"
    assert _copies() == {"notes/note.md": SHARED}
    assert result.total_deleted == 1


def test_a_source_on_another_volume_falls_back_to_copy_then_unlink(fleet, monkeypatch):
    path = _setup_deletion(fleet)

    def other_volume(real, src, dst, *args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", _on(os.rename, path, other_volume))
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    assert not path.exists() and result.total_deleted == 1 and err == ""
    assert _copies() == {"notes/note.md": SHARED}
    (copy,) = [p for p in statedb.deleted_copies_dir().rglob("note.md")]
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600


def test_a_failed_unlink_after_the_copy_is_not_retried_and_says_what_to_do(fleet, monkeypatch):
    # Another volume: the copy is safe but the original cannot be removed. The record
    # stays followed (base dropped, version stamped), so the next pull does not retry.
    path = _setup_deletion(fleet)
    real_unlink = Path.unlink

    def other_volume(real, src, dst, *args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def no_unlink(self, *args, **kwargs):
        if self == path:
            raise OSError(errno.EACCES, "Permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(os, "rename", _on(os.rename, path, other_volume))
    monkeypatch.setattr(Path, "unlink", no_unlink)
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    assert path.read_bytes() == SHARED and result.total_deleted == 0
    assert "not retried" in err and "republishes it" in err
    assert _copies() == {"notes/note.md": SHARED}
    base, stamp = _ledger_state("note.md")
    assert base is None and stamp == PEER
    second, _, _ = _run(fleet, monkeypatch, "quiet")
    assert second.total_deleted == 0 and path.read_bytes() == SHARED
    assert len(_copies()) == 1  # no second copy


@pytest.mark.parametrize("recreated", [False, True])
def test_an_edited_file_stays_in_recovery_when_hard_links_are_unavailable(
    fleet, monkeypatch, recreated
):
    path = _setup_deletion(fleet)

    def edit_then_move(real, src, dst, *args, **kwargs):
        path.write_bytes(b"typed after the check\n")
        return real(src, dst, *args, **kwargs)

    def no_links(*args, **kwargs):
        if recreated:
            path.write_bytes(b"a brand new file\n")
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "rename", _on(os.rename, path, edit_then_move))
    monkeypatch.setattr(os, "link", no_links)
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    # A failed no-clobber restoration never falls back to overwriting by rename.
    if recreated:
        assert path.read_bytes() == b"a brand new file\n"
    else:
        assert not path.exists()
    assert _copies() == {"notes/note.md": b"typed after the check\n"}
    assert (result.total_deleted, result.total_failed) == (0, 1)
    assert "changed while it was being deleted" in err
    folder = next(statedb.deleted_copies_dir().iterdir())
    assert f"moved file kept in recovery folder {folder}" in err


def test_a_cross_volume_delete_that_sees_the_file_change_keeps_it_and_drops_the_copy(
    fleet, monkeypatch
):
    path = _setup_deletion(fleet)
    real_flush = pullflow.fsutil.fsync_dir

    def other_volume(real, src, dst, *args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def flush_then_touch(target):
        real_flush(target)
        if (
            statedb.deleted_copies_dir() in Path(target).parents
            and (Path(target) / path.name).exists()
        ):
            path.write_bytes(b"edited during the copy\n")

    monkeypatch.setattr(os, "rename", _on(os.rename, path, other_volume))
    monkeypatch.setattr(pullflow.fsutil, "fsync_dir", flush_then_touch)
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    assert path.read_bytes() == b"edited during the copy\n"
    assert (result.total_deleted, result.total_failed) == (0, 1)
    assert "local file kept" in err and _copies() == {}


# ── previews: a dry run says what a pull would do and writes nothing ───


def test_a_dry_run_lists_deletions_and_why_a_file_stays_and_writes_nothing(fleet, monkeypatch):
    gone = _put_local(fleet, "gone.md", SHARED, 1_600_000_000)
    edited = _put_local(fleet, "edited.md", b"edited since\n", 1_600_000_000)
    _record(("gone.md", SHARED), ("edited.md", SHARED))
    _seed(
        fleet,
        {},
        tombstones={
            "notes:gone.md": _tombstone(SHARED),
            "notes:edited.md": _tombstone(SHARED),
        },
    )
    before = _snapshot(fleet.root)

    result, _, (out, err) = _run(fleet, monkeypatch, "attended", dry_run=True, verbose=True)

    assert _snapshot(fleet.root) == before  # no state.db write, no recovery folder, no reaping
    assert gone.read_bytes() == SHARED and edited.read_bytes() == b"edited since\n"
    assert result.total_deleted == 0 and err == ""
    assert re.search(r"^\s+delete\s+gone\.md$", out, re.M)
    assert "1 would be deleted" in out
    assert "keep (its bytes differ from the deleted version): Peer B/notes/edited.md" in out
    assert not re.search(r"delete\s+edited\.md", out)


def test_a_dry_run_names_older_peer_edits_and_conflict_copies_already_kept(fleet, monkeypatch):
    _seed(fleet, {"old.md": b"peer edit\n", "kept.md": b"peer kept\n"})
    _put_local(fleet, "old.md", b"base\n", 1_800_000_000)
    _record(("old.md", b"base\n"))
    _put_local(fleet, "kept.md", b"mine\n", 1_600_000_000)
    (fleet.local / "kept.sync-conflict-20260421-120000-v1-bbbbbbbb.md").write_bytes(b"peer kept\n")
    before = _snapshot(fleet.root)

    _, _, (out, _) = _run(fleet, monkeypatch, "attended", dry_run=True)

    assert _snapshot(fleet.root) == before
    assert "older (peer edit has an older timestamp)  old.md" in out
    assert "kept (conflict copy already saved)  kept.md" in out
    assert "1 older peer edits would not be applied" in out
    assert "1 conflict copies already kept" in out


def test_a_dry_run_creates_no_database(fleet, monkeypatch):
    _seed(fleet, {"note.md": b"peer content\n"})
    assert not statedb.db_path().exists()
    _run(fleet, monkeypatch, "attended", dry_run=True, verbose=True)
    assert not statedb.db_path().exists() and not statedb.deleted_copies_dir().exists()


def test_a_dry_run_never_reaps_old_recovery_folders(fleet, monkeypatch):
    old = statedb.deleted_copies_dir() / "20200101-000000-abcd" / NOTES
    old.mkdir(parents=True)
    (old / "x.md").write_bytes(b"x")
    _seed(fleet, {"note.md": b"peer content\n"})
    _run(fleet, monkeypatch, "attended", dry_run=True)
    assert (old / "x.md").exists()
    # A real pull does reap it: hooks never run `mm gc`.
    _run(fleet, monkeypatch, "quiet")
    assert not (old / "x.md").exists()


def test_fail_mode_preflight_does_not_refuse_for_a_deletion(fleet, monkeypatch):
    _setup_deletion(fleet, "gone.md")
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet", conflict_mode="fail")
    assert err == "" and result.total_deleted == 1


# ── what is recorded and what never is ─────────────────────────────────


def _row_kinds(ledger, path):
    return {p: e.kind for p, e in ledger.entries(NOTES, path).items()}


@pytest.mark.parametrize("case", ["written", "merged", "conflicted", "kept", "matched"])
def test_a_base_is_recorded_after_an_outcome_that_agrees_with_the_peer(fleet, monkeypatch, case):
    name = "events.jsonl" if case == "merged" else "note.md"
    remote = b'{"remote":1}\n' if case == "merged" else b"peer content\n"
    _seed(fleet, {name: remote})
    if case == "kept":
        _put_local(fleet, name, b"local content\n", 1_600_000_000)
        (fleet.local / KEPT_SIDECAR).write_bytes(remote)
    elif case == "matched":
        _put_local(fleet, name, remote, 1_600_000_000)
    elif case != "written":
        local = b'{"local":1}\n' if case == "merged" else b"local content\n"
        _put_local(fleet, name, local, 1_600_000_000)

    _run(fleet, monkeypatch, "quiet")

    ledger = _committed()
    assert ledger.base(PEER, NOTES, name) == _sha(remote)
    kind = "seen" if case in {"conflicted", "kept"} else "held"
    assert _row_kinds(ledger, name)[_sha(remote)[:16]] == kind
    if case == "merged":  # a union holds both sides it was built from
        assert _row_kinds(ledger, name)[_sha(b'{"local":1}\n')[:16]] == "held"


def _case_failed(fleet):
    peer, _ = _seed(fleet, {"note.md": b"peer content\n"})
    key = blob_key(PEER, peer["sources"][NOTES]["files"]["note.md"]["sha256"])
    raw = fleet.backend.get(key)
    fleet.backend.put(key, raw[:-1] + bytes([raw[-1] ^ 1]))


def _case_symlink(fleet):
    _seed(fleet, {"note.md": b"peer content\n"})
    target = fleet.root / "outside.md"
    target.write_bytes(b"outside\n")
    (fleet.local / "note.md").symlink_to(target)


def _case_local_newer(fleet):
    _seed(fleet, {"note.md": b"peer content\n"})
    _put_local(fleet, "note.md", b"local content\n", 1_800_000_000)


@pytest.mark.parametrize("link_at", ["leaf", "descendant", "source-root"])
def test_matching_local_symlink_is_skipped_but_source_root_link_can_settle(
    fleet, monkeypatch, link_at
):
    name = "skills/AGENTS.md" if link_at == "descendant" else "AGENTS.md"
    data = b"shared agent instructions\n"
    _seed(fleet, {name: data})
    shared = fleet.root / "shared-config"
    shared.mkdir()
    target = shared / "AGENTS.md"
    target.write_bytes(data)
    if link_at == "source-root":
        fleet.config["sync"]["sources"][0]["path"] = str(fleet.local / "linked-root")
        link = fleet.local / "linked-root"
        link.symlink_to(shared, target_is_directory=True)
    elif link_at == "descendant":
        link = fleet.local / "skills"
        link.symlink_to(shared, target_is_directory=True)
    else:
        link = fleet.local / name
        link.symlink_to(target)
    real_flush, flushed = pullflow.fsutil.fsync_file, []

    def flush_file(path):
        flushed.append(Path(path).resolve())
        return real_flush(path)

    monkeypatch.setattr(pullflow.fsutil, "fsync_file", flush_file)
    for attempt in range(2):
        flushed.clear()
        result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
        assert result.durability_fsync_failures == 0
        assert result.total_failed == result.total_applied == 0
        assert err == "" and link.is_symlink() and target.read_bytes() == data
        ledger = _committed()
        if link_at == "source-root":
            assert result.total_skipped == 0
            assert ledger.base(PEER, NOTES, name) == _sha(data)
            assert flushed == ([target] if attempt == 0 else [])
        else:
            assert result.total_skipped == 1
            assert ledger.base(PEER, NOTES, name) is None
            assert _row_kinds(ledger, name) == {}
            assert flushed == []


def _case_stale(fleet):
    _seed(fleet, {"note.md": b"an old version\n"})
    _put_local(fleet, "note.md", b"the newest version\n", 1_800_000_000)
    ledger = pullplan.Ledger()
    ledger.hold(NOTES, "note.md", _sha(b"an old version\n"))
    ledger.hold(NOTES, "note.md", _sha(b"the newest version\n"))
    assert core.commit_ledger(ledger) is None


@pytest.mark.parametrize(
    "case", [_case_failed, _case_symlink, _case_local_newer, _case_stale], ids=lambda f: f.__name__
)
def test_no_base_is_recorded_after_failed_symlink_local_newer_or_stale(fleet, monkeypatch, case):
    case(fleet)
    before = {k: v for k, v in _committed().seen.items()}

    result, _, _ = _run(fleet, monkeypatch, "quiet")

    ledger = _committed()
    assert ledger.base(PEER, NOTES, "note.md") is None
    assert {k: v for k, v in ledger.seen.items()} == before  # not even a seen version
    if case is _case_failed:
        assert result.total_failed == 1
    assert result.total_written == result.total_merged == result.total_conflicted == 0


def test_no_base_is_recorded_for_a_blob_that_has_not_arrived_yet(fleet, syncthing, monkeypatch):
    peer, _ = _seed(fleet, {"note.md": b"peer content\n"})
    syncthing.hide(_blob(peer, "note.md"))
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert result.total_waiting == 1
    assert _committed().base(PEER, NOTES, "note.md") is None
    syncthing.arrive(_blob(peer, "note.md"))
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert result.total_written == 1
    assert _committed().base(PEER, NOTES, "note.md") == _sha(b"peer content\n")


# ── inline keep-local settles only when the pull is not aborted ────────


def _two_conflicts(fleet):
    _seed(fleet, {"a.md": b"peer a\n", "b.md": b"peer b\n"})
    for name in ("a.md", "b.md"):
        _put_local(fleet, name, f"local {name}\n".encode(), 1_600_000_000)


def test_inline_keep_local_settles_at_the_end_of_the_pull_and_stays_quiet_after(fleet, monkeypatch):
    asked: list[str] = []

    def keep(rel_path, *args, **kwargs):
        asked.append(rel_path)
        return "keep-canonical", None

    _two_conflicts(fleet)
    monkeypatch.setattr(pullflow, "_prompt_conflict_choice", keep)
    result, _, _ = _run(fleet, monkeypatch, "attended", conflict_mode="prompt")
    assert sorted(asked) == ["a.md", "b.md"]
    assert result.total_skipped == 2 and result.total_conflicted == 0
    ledger = _committed()
    for name, remote in (("a.md", b"peer a\n"), ("b.md", b"peer b\n")):
        assert ledger.base(PEER, NOTES, name) == _sha(remote)
        assert _row_kinds(ledger, name)[_sha(remote)[:16]] == "seen"  # not built on it
    # The decision sticks: nothing prompts again.
    asked.clear()
    result, _, _ = _run(fleet, monkeypatch, "attended", conflict_mode="prompt")
    assert asked == [] and result.total_conflicted == 0


def test_an_aborted_pull_drops_the_inline_keep_local_settlements(fleet, monkeypatch):
    calls: list[str] = []

    def keep_then_abort(rel_path, *args, **kwargs):
        calls.append(rel_path)
        return ("keep-canonical", None) if len(calls) == 1 else ("abort", None)

    _two_conflicts(fleet)
    monkeypatch.setattr(pullflow, "_prompt_conflict_choice", keep_then_abort)
    with pytest.raises(typer.Abort):
        _run(fleet, monkeypatch, "attended", conflict_mode="prompt")
    kept = calls[0]
    ledger = _committed()
    assert ledger.base(PEER, NOTES, kept) is None  # half-made decisions are not broadcast
    assert _row_kinds(ledger, kept) == {}


# ── an unreadable or unwritable ledger pauses the new rule, never the pull ──


def _break_database(failure, monkeypatch):
    path = statedb.db_path()
    if failure == "corrupt":
        path.write_bytes(b"not a database")
    elif failure == "too-new":
        with sqlite3.connect(path) as raw:
            raw.execute("UPDATE meta SET value='9' WHERE key='compat_version'")
    elif failure == "busy":
        monkeypatch.setattr(statedb, "BUSY_TIMEOUT_MS", 50)
        locker = sqlite3.connect(path, isolation_level=None)
        locker.execute("BEGIN EXCLUSIVE")
        return locker
    elif failure == "interrupted":

        def hot(conn):
            raise statedb.StateDBHotJournal("SQLITE_READONLY_ROLLBACK")

        monkeypatch.setattr(statedb, "read_seen", hot)
    elif failure == "unavailable":
        monkeypatch.setitem(sys.modules, "sqlite3", None)
    return None


@pytest.mark.parametrize("failure", ["busy", "too-new", "corrupt", "interrupted", "unavailable"])
def test_each_database_failure_class_falls_back_to_todays_rule_and_deletes_nothing(
    fleet, monkeypatch, failure
):
    gone = _setup_deletion(fleet, "gone.md")  # a base is recorded while the database is fine
    _seed(
        fleet,
        {"new.md": b"fresh from the peer\n"},
        tombstones={"notes:gone.md": _tombstone(SHARED)},
    )
    locker = _break_database(failure, monkeypatch)
    try:
        result, _, (out, err) = _run(fleet, monkeypatch, "attended")
    finally:
        if locker is not None:
            locker.close()

    word = {"too-new": "too-new", "interrupted": "interrupted"}.get(failure, failure)
    assert result.ledger_cause == word
    # The pull still succeeded with today's rule...
    assert (fleet.local / "new.md").read_bytes() == b"fresh from the peer\n"
    assert result.total_written == 1 and result.total_failed == 0
    # ...and deleted nothing: the file a hashed record names is still there.
    assert gone.read_bytes() == SHARED and result.total_deleted == 0 and _copies() == {}
    # One notice names the cause and says what is paused.
    notice = pullflow.ledger_notice(result)
    assert notice in err
    assert "Pull ledger unavailable" in notice
    assert errors.LEDGER_URL in notice
    assert notice.startswith("mm: notice: ")


@pytest.mark.parametrize(
    "cause, text",
    [
        ("busy", "another mm command is using state.db; retry in a moment"),
        ("too-new", "state.db is from a newer mm; run mm update"),
        ("corrupt", "state.db is damaged; move it aside as the README describes"),
        ("interrupted", "an interrupted mm left an unfinished write in state.db; run mm status"),
        ("unavailable", "this Python cannot load sqlite3"),
        ("unreadable", "state.db could not be accessed"),
        ("anything-else", "state.db could not be accessed"),
    ],
)
def test_the_ledger_notice_names_each_cause(cause, text):
    notice = pullflow.ledger_notice(pullflow.PullResult(ledger_cause=cause))
    assert notice.startswith(f"mm: notice: {text}.")
    assert notice.endswith(f"See {errors.LEDGER_URL}.")
    assert pullflow.ledger_notice(pullflow.PullResult()) == ""


def test_an_absent_database_is_silent(fleet, monkeypatch):
    _seed(fleet, {"note.md": b"peer content\n"})
    assert not statedb.db_path().exists()
    result, _, (_, err) = _run(fleet, monkeypatch, "attended")
    assert result.ledger_cause is None and err == ""
    assert result.total_written == 1


def test_a_version_one_database_without_the_tables_is_silent(fleet, monkeypatch):
    statedb.DB_DIR.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(statedb.db_path())
    for statement in statedb.MIGRATIONS[0][0]:
        raw.execute(statement)
    raw.execute("INSERT INTO meta (key, value) VALUES ('compat_version', '1')")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    _seed(fleet, {"note.md": b"peer content\n"})

    result, _, (_, err) = _run(fleet, monkeypatch, "attended")

    assert result.ledger_cause is None and err == ""
    assert result.total_written == 1
    # The write path migrated it, so the base is now recorded.
    assert _committed().base(PEER, NOTES, "note.md") == _sha(b"peer content\n")


def test_a_failing_commit_of_a_normal_pull_reports_the_cause_and_fails_nothing(fleet, monkeypatch):
    _seed(fleet, {"note.md": b"peer content\n"})
    failure = statedb.StateDBBusy("SQLITE_BUSY")
    with monkeypatch.context() as broken:
        broken.setattr(core, "commit_ledger", lambda ledger: failure)
        result, _, (_, err) = _run(fleet, broken, "attended")
    assert result.ledger_cause == "busy"
    assert (result.total_written, result.total_failed) == (1, 0)
    assert pullflow.ledger_notice(result) in err
    # Nothing was recorded, so the next pull derives it again.
    assert _committed().base(PEER, NOTES, "note.md") is None
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert result.ledger_cause is None
    assert _committed().base(PEER, NOTES, "note.md") == _sha(b"peer content\n")


# ── the planner and a real pull agree on every branch ──────────────────
#
# `mm pull --dry-run` and `--conflict-mode fail` must predict what the real pull does, so
# both call `pullplan.decide`. Allowed differences, pinned below:
#   * a predicted `merge` is the upper bound: a real union can be a no-op ("unchanged");
#   * blob and decrypt failures are not predicted (a corrupt blob predicts `write`, really
#     `failed`, and that failure blocks the same peer's deletions): pinned in its own test;
#   * "kept" and "held" are quiet in a real pull (nothing is counted) and named in a preview.

HELD_ROW_NAME = "removed-here.md"
OLD_RELS = {"old-version.md"}


def _parity_world(fleet):
    """One file per branch of the rule. Returns (own lookup, {name: (predicted, real)})."""

    def local(name, data, mtime):
        return _put_local(fleet, name, data, mtime)

    remote: dict[str, bytes] = {}
    ancestors: dict[str, list[bytes]] = {}
    rows: list[tuple[str, bytes]] = []

    remote["write.md"] = b"brand new\n"

    local("ff.md", b"ff v0\n", 1_600_000_000)
    rows.append(("ff.md", b"ff v0\n"))
    remote["ff.md"], ancestors["ff.md"] = b"ff v1\n", [b"ff v0\n"]

    remote["matched.md"] = b"same\n"
    local("matched.md", b"same\n", 1_600_000_000)

    local("skip.md", b"mine\n", 1_600_000_000)
    remote["skip.md"] = b"theirs\n"
    rows.append(("skip.md", b"theirs\n"))

    remote[HELD_ROW_NAME] = b"removed on purpose\n"

    local("stale.md", b"the newest\n", 1_800_000_000)
    remote["stale.md"] = b"an old one\n"

    local("older.md", b"older v0\n", 1_800_000_000)
    rows.append(("older.md", b"older v0\n"))
    remote["older.md"] = b"older v1\n"

    local("fb-conflict.md", b"mine\n", 1_600_000_000)
    remote["fb-conflict.md"] = b"theirs\n"
    local("fb-newer.md", b"mine\n", 1_800_000_000)
    remote["fb-newer.md"] = b"theirs\n"
    local("fb-merge.jsonl", b'{"a":1}\n', 1_600_000_000)
    remote["fb-merge.jsonl"] = b'{"b":2}\n'
    local("noop-merge.jsonl", b'{"a":1}\n{"b":2}\n', 1_600_000_000)
    remote["noop-merge.jsonl"] = b'{"b":2}\n'

    local("both-conflict.md", b"edited here\n", 1_600_000_000)
    rows.append(("both-conflict.md", b"the shared one\n"))
    remote["both-conflict.md"] = b"edited there\n"
    local("both-merge.jsonl", b'{"here":1}\n', 1_600_000_000)
    rows.append(("both-merge.jsonl", b'{"shared":0}\n'))
    remote["both-merge.jsonl"] = b'{"there":1}\n'

    local("kept.md", b"mine\n", 1_600_000_000)
    remote["kept.md"] = b"theirs, saved\n"
    (fleet.local / "kept.sync-conflict-20260421-120000-v1-bbbbbbbb.md").write_bytes(
        remote["kept.md"]
    )

    expectations = {
        "write.md": (("write", ""), "written"),
        "ff.md": (("write", "fast-forward"), "written"),
        "matched.md": (("unchanged", ""), "unchanged"),
        "skip.md": (("unchanged", ""), "unchanged"),
        HELD_ROW_NAME: (("skip", "removed here; not restored"), "unchanged"),
        "stale.md": (("unchanged", ""), "unchanged"),
        "older.md": (("older", "peer edit has an older timestamp"), "older"),
        "fb-conflict.md": (("conflict", ""), "conflicted"),
        "fb-newer.md": (("skip", "local newer"), "skipped"),
        "fb-merge.jsonl": (("merge", ""), "merged"),
        "noop-merge.jsonl": (("merge", ""), "unchanged"),  # allowed: a no-op union
        "both-conflict.md": (("conflict", ""), "conflicted"),
        "both-merge.jsonl": (("merge", ""), "merged"),
        "kept.md": (("kept", "conflict copy already saved"), "kept"),
        "deleted.md": (("delete", ""), "deleted"),
    }
    # The same file shape for the branches that need a deletion record.
    local("deleted.md", SHARED, 1_600_000_000)
    rows.append(("deleted.md", SHARED))

    _seed(fleet, remote, tombstones={"notes:deleted.md": _tombstone(SHARED)})
    peer = core._fetch_remote_manifest(fleet.backend, PEER, PASSPHRASE, MEMORY_KB).manifest
    files = peer["sources"][NOTES]["files"]
    for name, data in ancestors.items():
        files[name]["from"] = [_sha(b)[:16] for b in data]
    files["older.md"]["mtime"] = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).isoformat()
    _republish(fleet, peer)

    ledger = pullplan.Ledger()
    for name, data in rows:
        ledger.settle(PEER, NOTES, name, _sha(data))
    # `stale.md`: this machine held an old version, then the newest; the peer offers the old.
    ledger.hold(NOTES, "stale.md", _sha(b"an old one\n"))
    ledger.hold(NOTES, "stale.md", _sha(b"the newest\n"))
    # `removed-here.md`: held once, then removed here on purpose.
    ledger.hold(NOTES, HELD_ROW_NAME, _sha(remote[HELD_ROW_NAME]))
    assert core.commit_ledger(ledger) is None

    def lookup(src, rel):
        return (True, True) if rel == HELD_ROW_NAME else (False, False)

    return lookup, expectations


def _predict_all(fleet, lookup):
    peer = core._fetch_remote_manifest(fleet.backend, PEER, PASSPHRASE, MEMORY_KB).manifest
    planner, unknown = pullflow._plan_pull(
        [{"device_id": PEER, "device_name": "Peer B"}],
        {PEER: peer},
        {NOTES: {"path": fleet.local, "type": "generic"}},
        None,
        manifest.collect_tombstones([PEER], lambda did: peer),
        _committed(),
        lookup,
        LOCAL,
    )
    assert unknown == []
    return {p.rel_path: (p.outcome, p.reason) for p in planner.predictions}


def test_the_planner_and_a_real_pull_agree_on_every_branch(fleet, monkeypatch):
    lookup, expectations = _parity_world(fleet)
    monkeypatch.setattr(pullflow, "_own_lookup", lambda *args: lookup)
    before = _snapshot(fleet.root)

    predicted = _predict_all(fleet, lookup)
    assert _snapshot(fleet.root) == before  # predicting touched nothing
    assert predicted == {name: want for name, (want, _) in expectations.items()}

    recorded: dict[str, str] = {}
    real_record = pullflow._ApplyReporter.record

    def spy(self, outcome):
        first = self.recorded is None
        real_record(self, outcome)
        if first:
            recorded[self.rel_path] = outcome

    monkeypatch.setattr(pullflow._ApplyReporter, "record", spy)
    result, _, _ = _run(fleet, monkeypatch, "quiet")

    acted = {r["rel_path"]: r["action"] for r in pullhistory.read_records()}
    older = {e.rel_path for e in result.older}
    real = {
        name: "older" if name in older else acted.get(name) or recorded.get(name, "unchanged")
        for name in expectations
    }
    assert real == {name: want for name, (_, want) in expectations.items()}

    # The relation itself: every real word is what the prediction means, bar the pinned gaps.
    meaning = {
        ("write", ""): {"written"},
        ("write", "fast-forward"): {"written"},
        ("merge", ""): {"merged", "unchanged"},  # a predicted merge can be a real no-op
        ("conflict", ""): {"conflicted"},
        ("kept", "conflict copy already saved"): {"kept"},  # counts nothing, warns nothing
        ("older", "peer edit has an older timestamp"): {"older"},
        ("delete", ""): {"deleted"},
        ("skip", "local newer"): {"skipped"},
        ("skip", "removed here; not restored"): {"unchanged"},
        ("unchanged", ""): {"unchanged"},
    }
    for name, want in predicted.items():
        assert real[name] in meaning[want], (name, want, real[name])
    # Totals agree with the per-file words.
    assert (result.total_written, result.total_merged, result.total_conflicted) == (2, 2, 2)
    assert (result.total_skipped, result.total_failed, result.total_deleted) == (1, 0, 1)
    assert [e.rel_path for e in result.older] == ["older.md"]
    assert not (fleet.local / HELD_ROW_NAME).exists()
    assert not (fleet.local / "deleted.md").exists()


def test_a_dry_run_over_every_branch_writes_nothing(fleet, monkeypatch):
    lookup, _ = _parity_world(fleet)
    monkeypatch.setattr(pullflow, "_own_lookup", lambda *args: lookup)
    before = _snapshot(fleet.root)
    result, summaries, (out, _) = _run(fleet, monkeypatch, "attended", dry_run=True, verbose=True)
    assert _snapshot(fleet.root) == before
    assert result.total_deleted == 0 and summaries == [("", "")]
    assert "1 would be deleted" in out and "1 older peer edits would not be applied" in out


def test_blob_failures_are_not_predicted_and_block_that_peers_deletions(fleet, monkeypatch):
    # The preview cannot know a blob will fail to decrypt: it predicts `write` (and a
    # deletion), the pull fails the file and, with that peer incomplete, deletes nothing.
    gone = _setup_deletion(fleet, "gone.md")
    peer, _ = _seed(
        fleet, {"bad.md": b"will not decrypt\n"}, tombstones={"notes:gone.md": _tombstone(SHARED)}
    )
    key = blob_key(PEER, peer["sources"][NOTES]["files"]["bad.md"]["sha256"])
    raw = fleet.backend.get(key)
    fleet.backend.put(key, raw[:-1] + bytes([raw[-1] ^ 1]))

    predicted = _predict_all(fleet, lambda src, rel: (False, False))
    assert predicted["bad.md"] == ("write", "") and predicted["gone.md"][0] == "delete"

    result, _, _ = _run(fleet, monkeypatch, "quiet")

    assert result.total_failed == 1 and result.total_deleted == 0
    assert gone.read_bytes() == SHARED and _copies() == {}


# ── a base is never more durable than the bytes it describes ───────────


def test_a_failed_directory_flush_commits_no_base(fleet, monkeypatch):
    from mind_meld import fsutil

    _seed(fleet, {"note.md": b"peer content\n"})

    def boom(path):
        raise errors.StorageError("simulated fsync failure")

    monkeypatch.setattr(fsutil, "fsync_dir", boom)
    result, _, _ = _run(fleet, monkeypatch, "quiet")

    assert result.durability_fsync_failures == 1 and result.total_written == 1
    # After a power loss the bytes may be damaged: no base may claim they are the peer's.
    assert _committed().base(PEER, NOTES, "note.md") is None


def test_a_file_that_cannot_be_flushed_loses_its_base_but_the_others_keep_theirs(
    fleet, monkeypatch
):
    from mind_meld import fsutil

    _seed(fleet, {"bad.md": b"will not flush\n", "good.md": b"flushes fine\n"})
    real = fsutil.fsync_file
    flushed = []
    fail = True

    def flaky(path):
        flushed.append(Path(path))
        if fail and Path(path).name == "bad.md":
            raise OSError(errno.EIO, "Input/output error")
        return real(path)

    monkeypatch.setattr(fsutil, "fsync_file", flaky)
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")

    assert result.total_written == 2
    assert result.durability_fsync_failures == 1
    assert f"durability fsync failed on {fleet.local / 'bad.md'}" in err
    ledger = _committed()
    assert ledger.base(PEER, NOTES, "bad.md") is None  # re-derived by the next pull
    assert _row_kinds(ledger, "bad.md") == {}
    assert ledger.base(PEER, NOTES, "good.md") == _sha(b"flushes fine\n")
    # Matching bytes on retry do not prove the failed flush became durable.
    flushed.clear()
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    assert flushed == [fleet.local / "bad.md"]  # good.md is already settled
    _counts(result, durability_fsync_failures=1)
    assert "durability fsync failed" in err
    assert _committed().base(PEER, NOTES, "bad.md") is None
    assert _row_kinds(_committed(), "bad.md") == {}

    flushed.clear()
    fail = False
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    _counts(result)
    assert flushed == [fleet.local / "bad.md"]
    assert _committed().base(PEER, NOTES, "bad.md") == _sha(b"will not flush\n")


def test_kept_only_retry_retries_failed_directory_flush(fleet, monkeypatch):
    _seed(fleet, {"note.md": b"peer bytes\n"})
    local = _put_local(fleet, "note.md", b"my bytes\n", 1_600_000_000)
    real_file, real_dir = pullflow.fsutil.fsync_file, pullflow.fsutil.fsync_dir
    files, parents = [], []
    fail = True

    def flush_file(path):
        files.append(Path(path))
        return real_file(path)

    def flush_dir(path):
        parents.append(Path(path))
        if fail and Path(path) == fleet.local:
            raise errors.StorageError("simulated directory flush failure")
        return real_dir(path)

    monkeypatch.setattr(pullflow.fsutil, "fsync_file", flush_file)
    monkeypatch.setattr(pullflow.fsutil, "fsync_dir", flush_dir)
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert result.total_conflicted == result.durability_fsync_failures == 1
    assert _committed().base(PEER, NOTES, "note.md") is None
    kept = next(fleet.local.glob("note.sync-conflict-*.md"))
    history = list(pullhistory.read_records())

    files.clear()
    parents.clear()
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    _counts(result, durability_fsync_failures=1)
    assert files == [kept] and fleet.local in parents
    assert "durability fsync failed" in err
    assert _committed().base(PEER, NOTES, "note.md") is None
    assert _row_kinds(_committed(), "note.md") == {}

    fail = False
    files.clear()
    parents.clear()
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    _counts(result)
    assert files == [kept] and fleet.local in parents
    assert _committed().base(PEER, NOTES, "note.md") == _sha(b"peer bytes\n")
    assert list(pullhistory.read_records()) == history
    assert local.read_bytes() == b"my bytes\n" and kept.read_bytes() == b"peer bytes\n"
    assert list(fleet.local.glob("note.sync-conflict-*.md")) == [kept]


@pytest.mark.parametrize("agreement", ["apply-time-equality", "merge-no-op"])
@pytest.mark.parametrize("failed_flush", ["file", "directory"])
def test_agreeing_apply_flushes_before_settling(fleet, monkeypatch, agreement, failed_flush):
    remote = b'{"id":1}\n'
    union = remote + b'{"id":2}\n'
    name = "note.md" if agreement == "apply-time-equality" else "notes.jsonl"
    _seed(fleet, {name: remote})
    local = _put_local(fleet, name, union, 1_600_000_000)
    real_apply = pullflow._apply_incoming_file
    applied = []

    def apply(*args, **kwargs):
        if agreement == "apply-time-equality":
            # An agent makes the file equal after classification selected a download.
            local.write_bytes(remote)
        outcome = real_apply(*args, **kwargs)
        applied.append(outcome)
        return outcome

    real_file, real_dir = pullflow.fsutil.fsync_file, pullflow.fsutil.fsync_dir
    files, parents = [], []
    fail = True

    def flush_file(path):
        files.append(Path(path))
        if fail and failed_flush == "file" and Path(path) == local:
            raise OSError(errno.EIO, "simulated file flush failure")
        return real_file(path)

    def flush_dir(path):
        parents.append(Path(path))
        if fail and failed_flush == "directory" and Path(path) == local.parent:
            raise errors.StorageError("simulated directory flush failure")
        return real_dir(path)

    monkeypatch.setattr(pullflow, "_apply_incoming_file", apply)
    monkeypatch.setattr(pullflow.fsutil, "fsync_file", flush_file)
    monkeypatch.setattr(pullflow.fsutil, "fsync_dir", flush_dir)
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert applied == ["unchanged"]
    _counts(result, durability_fsync_failures=1)
    assert files == [local] and local.parent in parents
    assert _committed().base(PEER, NOTES, name) is None
    assert _row_kinds(_committed(), name) == {}

    fail = False
    files.clear()
    parents.clear()
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    _counts(result)
    assert files == [local] and local.parent in parents
    assert _committed().base(PEER, NOTES, name) == _sha(remote)
    assert local.read_bytes() == (remote if agreement == "apply-time-equality" else union)
    assert not list(pullhistory.read_records())


def test_an_interrupted_pull_still_commits_what_it_settled(fleet, monkeypatch):
    _seed(fleet, {"a.md": b"first\n", "b.md": b"second\n"})
    real = pullflow._apply_incoming_file
    calls: list[str] = []

    def interrupt_second(*args, **kwargs):
        calls.append(kwargs["rel_path"])
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(pullflow, "_apply_incoming_file", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        _run(fleet, monkeypatch, "quiet")

    ledger = _committed()
    first, second = calls
    assert ledger.base(PEER, NOTES, first) is not None
    assert ledger.base(PEER, NOTES, second) is None


# Review regressions: single-machine writers and recovery publication failures.
def test_fast_forward_keeps_a_same_size_write_that_preserved_mtime(fleet, monkeypatch):
    _seed(fleet, {"note.md": b"remote\n"})
    path = _put_local(fleet, "note.md", b"before\n", 1_600_000_000)
    _record(("note.md", b"before\n"))
    real = pullflow._apply_fast_forward

    def edit_before_recheck(local_path, *args, **kwargs):
        before = local_path.stat()
        local_path.write_bytes(b"edited\n")
        os.utime(local_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return real(local_path, *args, **kwargs)

    monkeypatch.setattr(pullflow, "_apply_fast_forward", edit_before_recheck)
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert path.read_bytes() == b"edited\n"
    assert result.total_conflicted == 1


@pytest.mark.parametrize("copy_only", [False, True])
def test_recovery_publication_never_overwrites_an_occupied_destination(tmp_path, copy_only):
    local, recovery = tmp_path / "canonical", tmp_path / "recovery"
    local.write_bytes(b"second file")
    recovery.write_bytes(b"first recovered file")
    move = pullflow._copy_then_unlink if copy_only else pullflow._move_into_copy
    with pytest.raises(OSError):
        move(local, recovery, _sha(b"second file"))
    assert recovery.read_bytes() == b"first recovered file"
    assert local.read_bytes() == b"second file"


@pytest.mark.parametrize("phase", ["chmod", "hash", "copy-unlink"])
def test_post_move_failure_names_the_recovery_folder_and_keeps_followed_evidence(
    fleet, monkeypatch, phase
):
    path = _setup_deletion(fleet)
    root = statedb.deleted_copies_dir()
    if phase == "chmod":
        real = os.chmod

        def fail_chmod(target, *args, **kwargs):
            if Path(target).name == path.name and root in Path(target).parents:
                raise OSError(errno.EIO, "injected chmod failure")
            return real(target, *args, **kwargs)

        monkeypatch.setattr(os, "chmod", fail_chmod)
    elif phase == "hash":
        real = pullflow.hash_file

        def fail_hash(target, *args, **kwargs):
            if root in Path(target).parents:
                raise OSError(errno.EIO, "injected hash failure")
            return real(target, *args, **kwargs)

        monkeypatch.setattr(pullflow, "hash_file", fail_hash)
    else:

        def edit_then_move(real, src, dst, *args, **kwargs):
            path.write_bytes(b"new edited bytes\n")
            return real(src, dst, *args, **kwargs)

        real_unlink = os.unlink

        def fail_unlink(target, *args, **kwargs):
            if root in Path(target).parents and Path(target).name == path.name:
                raise OSError(errno.EIO, "injected recovery unlink failure")
            return real_unlink(target, *args, **kwargs)

        monkeypatch.setattr(os, "rename", _on(os.rename, path, edit_then_move))
        monkeypatch.setattr(os, "unlink", fail_unlink)
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    folder = next(root.iterdir())
    assert str(folder) in err
    assert result.total_failed == 1
    assert _copies()["notes/note.md"] == (
        b"new edited bytes\n" if phase == "copy-unlink" else SHARED
    )
    if phase != "copy-unlink":
        assert not path.exists()
        assert _ledger_state("note.md") == (None, PEER)
        assert "local and recovery copy preserved" not in err


def test_moved_symlink_never_changes_its_target_permissions(fleet, monkeypatch):
    path = _setup_deletion(fleet)
    target = fleet.root / "private-target"
    target.write_bytes(b"private contents")
    target.chmod(0o644)

    def swap_then_move(real, src, dst, *args, **kwargs):
        path.unlink()
        path.symlink_to(target)
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", _on(os.rename, path, swap_then_move))
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert target.read_bytes() == b"private contents"
    assert result.total_failed == 1
    assert str(next(statedb.deleted_copies_dir().iterdir())) in err


def test_failed_hardlink_restore_does_not_turn_the_move_into_a_new_deletion(fleet, monkeypatch):
    path = _setup_deletion(fleet)

    def edit_then_move(real, src, dst, *args, **kwargs):
        path.write_bytes(b"typed after check")
        return real(src, dst, *args, **kwargs)

    def no_links(*args, **kwargs):
        raise OSError(errno.EPERM, "no hard links")

    monkeypatch.setattr(os, "rename", _on(os.rename, path, edit_then_move))
    monkeypatch.setattr(os, "link", no_links)
    _run(fleet, monkeypatch, "quiet")
    assert not path.exists()
    assert _ledger_state("note.md") == (None, PEER)


def test_every_recovery_directory_flush_is_attempted_and_failure_is_visible(fleet, monkeypatch):
    for name in ("first/a.md", "second/b.md"):
        _put_local(fleet, name, SHARED, 1_600_000_000)
        _record((name, SHARED))
    _seed(
        fleet,
        {},
        tombstones={f"notes:{name}": _tombstone(SHARED) for name in ("first/a.md", "second/b.md")},
    )
    real = pullflow.fsutil.fsync_dir
    flushed = []

    def fail_after_move(directory):
        directory = Path(directory)
        # Initial empty-directory flush succeeds; only the post-move flush fails.
        if any(directory.glob("*.md")):
            flushed.append(directory.name)
            raise errors.StorageError("injected recovery flush failure")
        return real(directory)

    monkeypatch.setattr(pullflow.fsutil, "fsync_dir", fail_after_move)
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    assert set(flushed) == {"first", "second"}
    assert result.total_deleted == 2
    assert result.durability_fsync_failures == 2
    assert "durability fsync failed" in err


@pytest.mark.parametrize("bad_roots", [[], ["."], "invalid", 1])
def test_malformed_own_root_ids_never_crash_or_prove_an_unchanged_root(fleet, bad_roots):
    lookup = pullflow._own_lookup(
        {"sources": {NOTES: {"files": {"note.md": {}}, "root_ids": bad_roots}}},
        {NOTES: fleet.config["sync"]["sources"][0]},
        {NOTES: {"path": fleet.local}},
    )
    assert lookup(NOTES, "note.md") == (True, False)


def test_commit_failure_after_fast_forward_does_not_claim_the_old_rule_ran(fleet, monkeypatch):
    _seed(fleet, {"note.md": b"remote\n"})
    path = _put_local(fleet, "note.md", b"before\n", 1_600_000_000)
    _record(("note.md", b"before\n"))
    monkeypatch.setattr(core, "commit_ledger", lambda ledger: statedb.StateDBBusy("busy"))
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    assert path.read_bytes() == b"remote\n" and result.total_written == 1
    assert "this pull used the old mtime rule" not in pullflow.ledger_notice(result)
    assert "deletions are paused" not in pullflow.ledger_notice(result)


def test_cross_volume_recheck_keeps_a_same_size_write_that_preserved_mtime(tmp_path, monkeypatch):
    path, copy_path = tmp_path / "original", tmp_path / "copy"
    path.write_bytes(b"before\n")
    before = path.stat()
    real = pullflow.fsutil.fsync_dir

    def edit_before_recheck(directory):
        path.write_bytes(b"edited\n")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return real(directory)

    monkeypatch.setattr(pullflow.fsutil, "fsync_dir", edit_before_recheck)
    outcome = pullflow._copy_then_unlink(path, copy_path, _sha(b"before\n"))
    assert outcome == "restored"
    assert path.read_bytes() == b"edited\n"
    assert not copy_path.exists()


def test_mixed_apply_and_deletion_keep_both_in_the_project_sync_log(fleet, monkeypatch):
    added, removed = "projects/demo/memory/new.md", "projects/demo/memory/gone.md"
    fleet.config["sync"]["sources"][0]["type"] = "claude"
    _put_local(fleet, removed, SHARED, 1_600_000_000)
    _record((removed, SHARED))
    _seed(fleet, {added: b"new content\n"}, tombstones={f"notes:{removed}": _tombstone(SHARED)})
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert result.total_written == result.total_deleted == 1
    log = (fleet.local / "projects/demo/.mind-meld-log.md").read_text()
    assert "memory/new.md" in log and "memory/gone.md" in log
    rows = [r for r in pullhistory.read_records() if r.get("rel_path") in {added, removed}]
    assert [(r["action"], r["rel_path"]) for r in rows] == [
        ("written", added),
        ("deleted", removed),
    ]


def test_kept_conflict_copy_is_flushed_before_recording_its_peer_base(fleet, monkeypatch):
    _seed(fleet, {"note.md": b"peer bytes\n"})
    _put_local(fleet, "note.md", b"my bytes\n", 1_600_000_000)
    kept = fleet.local / "note.sync-conflict-20260421-120000-v1-bbbbbbbb.md"
    kept.write_bytes(b"peer bytes\n")
    real_flush, flushed = pullflow.fsutil.fsync_file, []

    def record_flush(path):
        flushed.append(Path(path))
        return real_flush(path)

    monkeypatch.setattr(pullflow.fsutil, "fsync_file", record_flush)
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert result.total_conflicted == 0 and result.total_written == 0
    assert flushed == [kept]
    assert _committed().base(PEER, NOTES, "note.md") == _sha(b"peer bytes\n")


@pytest.mark.parametrize("recreated_link", [False, True])
def test_deletion_cancels_inline_keep_local_bump_and_deferred_settlement(
    fleet, monkeypatch, recreated_link
):
    path = _setup_deletion(fleet)
    ledger = _committed()
    ledger.deferred.append((PEER, NOTES, "note.md", _sha(b"some other peer edit"), ()))
    pending = {path.resolve(): 1_800_000_000.0}
    if recreated_link:
        target = fleet.root / "new-target"
        target.write_bytes(b"new unrelated contents")

        def move_then_recreate(real, src, dst, *args, **kwargs):
            result = real(src, dst, *args, **kwargs)
            path.symlink_to(target)
            return result

        monkeypatch.setattr(os, "rename", _on(os.rename, path, move_then_recreate))
    bumps = []
    monkeypatch.setattr(
        pullflow, "_bump_canonical_mtime_post_resolve", lambda *args: bumps.append(args)
    )
    deletion = pullflow._Deletion(PEER, "Peer B", NOTES, "note.md", fleet.local, path, _sha(SHARED))
    pullflow._run_deletions(
        [deletion],
        ledger=ledger,
        touched_parents=set(),
        per_source_results=[],
        quiet=True,
        verbose=False,
        pending_inline_bumps=pending,
    )
    pullflow._drain_inline_bumps(pending)
    ledger.flush_deferred()
    assert path.is_symlink() if recreated_link else not path.exists()
    assert bumps == []
    assert ledger.base(PEER, NOTES, "note.md") is None
    assert ledger.newest_stamp(NOTES, "note.md") == PEER


def test_remove_owned_copy_refuses_protected_root(synthetic_account_home, forbid_fs_mutations):
    path = synthetic_account_home / ".config/mind-meld/recovery"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"sentinel")
    owned = path.stat()
    calls = forbid_fs_mutations()
    with pytest.raises(pullflow.fsutil.PytestRealStateWriteError):
        pullflow._remove_owned_copy(path, owned)
    assert calls == []


def test_remove_owned_copy_tmp_control_and_replacement_refusal(tmp_path):
    path = tmp_path / "copy"
    path.write_bytes(b"ours")
    owned = path.stat()
    pullflow._remove_owned_copy(path, owned)
    assert not path.exists()
    path.write_bytes(b"ours again")
    owned = path.stat()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"someone else")
    replacement.replace(path)
    with pytest.raises(OSError, match="recovery destination changed"):
        pullflow._remove_owned_copy(path, owned)
    assert path.read_bytes() == b"someone else"


@pytest.mark.parametrize("raises", [False, True])
def test_recovery_cleanup_failure_warns_in_quiet_mode_and_degrades_the_result(
    fleet, monkeypatch, raises
):
    _seed(fleet, {})

    def fail_reaper(*args, **kwargs):
        if raises:
            raise RuntimeError("injected cleanup failure")
        return pullflow.retention.ReapOutcome(failed=2)

    monkeypatch.setattr(pullflow.retention, "_gc_deleted_copies", fail_reaper)
    result, _, (_, err) = _run(fleet, monkeypatch, "quiet")
    assert result.maintenance_failures == (1 if raises else 2)
    assert "recovery cleanup failed" in err
    assert str(statedb.deleted_copies_dir()) in err
    assert "mm gc" in err


def test_failed_reservation_cleanup_does_not_unlink_a_replacement(tmp_path, monkeypatch):
    local, recovered = tmp_path / "local", tmp_path / "recovered"
    local.write_bytes(b"canonical")

    def replace_reservation_then_fail(src, dst):
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"someone else's copy")
        replacement.replace(dst)
        raise OSError(errno.EIO, "rename failed")

    monkeypatch.setattr(os, "rename", replace_reservation_then_fail)
    with pytest.raises(OSError):
        pullflow._move_into_copy(local, recovered, _sha(b"canonical"))
    assert local.read_bytes() == b"canonical"
    assert recovered.read_bytes() == b"someone else's copy"


def test_a_late_commit_failure_does_not_hide_the_completed_deletion(fleet, monkeypatch):
    for name in ("a.md", "b.md"):
        _put_local(fleet, name, SHARED, 1_600_000_000)
        _record((name, SHARED))
    _seed(fleet, {}, tombstones={f"notes:{name}": _tombstone(SHARED) for name in ("a.md", "b.md")})
    real_commit, real_move = core.commit_ledger, pullflow._move_into_copy
    commits = []

    def first_commit_only(ledger):
        commits.append(1)
        return real_commit(ledger) if len(commits) == 1 else statedb.StateDBBusy("busy")

    def fail_second(local, copy_path, sha):
        if local.name == "b.md":
            raise OSError(errno.EACCES, "rename refused")
        return real_move(local, copy_path, sha)

    monkeypatch.setattr(core, "commit_ledger", first_commit_only)
    monkeypatch.setattr(pullflow, "_move_into_copy", fail_second)
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert result.total_deleted == result.total_failed == 1
    assert result.ledger_cause == "busy"
    assert "deleted nothing" not in pullflow.ledger_notice(result)
    assert "Completed file changes remain" in pullflow.ledger_notice(result)


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, RuntimeError])
@pytest.mark.parametrize("bookkeeping_raises", [False, True])
def test_interrupted_deletion_keeps_completed_history_folder_and_unexecuted_bases(
    fleet, monkeypatch, error_type, bookkeeping_raises
):
    names = [f"projects/demo/memory/{letter}.md" for letter in "abc"]
    added = "projects/demo/memory/new.md"
    fleet.config["sync"]["sources"][0]["type"] = "claude"
    for name in names:
        _put_local(fleet, name, SHARED, 1_600_000_000)
        _record((name, SHARED))
    _seed(
        fleet,
        {added: b"new content\n"},
        tombstones={f"notes:{name}": _tombstone(SHARED) for name in names},
    )
    real_move = pullflow._move_into_copy
    attempted, observed, bookkeeping_calls = [], [], []
    interruption = error_type("original deletion interruption")

    def interrupt_second(local, copy_path, sha):
        attempted.append(local)
        if len(attempted) == 2:
            raise interruption
        return real_move(local, copy_path, sha)

    real_bookkeeping = pullflow._record_source_bookkeeping

    def record_then_fail(result, src_data, **kwargs):
        real_bookkeeping(result, src_data, **kwargs)
        if result.outcomes["deleted"]:
            bookkeeping_calls.append(result)
            if bookkeeping_raises:
                raise ValueError("secondary bookkeeping failure")

    monkeypatch.setattr(pullflow, "_move_into_copy", interrupt_second)
    monkeypatch.setattr(pullflow, "_record_source_bookkeeping", record_then_fail)
    capture = {}
    with pytest.raises(error_type) as caught:
        _run(fleet, monkeypatch, "quiet", refusal=capture, result_observer=observed.append)
    assert caught.value is interruption
    assert len(observed) == 1
    result = observed[0]
    assert result.total_written == result.total_deleted == 1
    assert result.total_failed == 0
    folder = Path(result.deleted_folder)
    assert (folder / NOTES / names[0]).read_bytes() == SHARED
    assert not (fleet.local / names[0]).exists()
    assert _ledger_state(names[0]) == (None, PEER)
    for name in names[1:]:
        assert (fleet.local / name).read_bytes() == SHARED
        assert _ledger_state(name) == (_sha(SHARED), None)
    rows = [r for r in pullhistory.read_records() if r.get("rel_path") in {*names, added}]
    assert [(r["action"], r["rel_path"]) for r in rows] == [
        ("written", added),
        ("deleted", names[0]),
    ]
    assert len(bookkeeping_calls) == 1
    log = (fleet.local / "projects/demo/.mind-meld-log.md").read_text()
    assert "memory/new.md" in log and "memory/a.md" in log
    assert "memory/b.md" not in log and "memory/c.md" not in log


def test_interruption_inside_a_moved_file_keeps_its_stamp_without_claiming_success(
    fleet, monkeypatch
):
    for name in ("a.md", "b.md", "c.md"):
        _put_local(fleet, name, SHARED, 1_600_000_000)
        _record((name, SHARED))
    _seed(
        fleet,
        {},
        tombstones={f"notes:{name}": _tombstone(SHARED) for name in ("a.md", "b.md", "c.md")},
    )
    real_move = pullflow._move_into_copy
    observed = []
    interruption = KeyboardInterrupt("after inode move")

    def interrupt_after_second_move(local, copy_path, sha):
        outcome = real_move(local, copy_path, sha)
        if local.name == "b.md":
            raise interruption
        return outcome

    monkeypatch.setattr(pullflow, "_move_into_copy", interrupt_after_second_move)
    capture = {}
    with pytest.raises(KeyboardInterrupt) as caught:
        _run(fleet, monkeypatch, "quiet", refusal=capture, result_observer=observed.append)
    assert caught.value is interruption
    (result,) = observed
    assert result.total_deleted == 1
    folder = Path(result.deleted_folder)
    for name in ("a.md", "b.md"):
        assert (folder / NOTES / name).read_bytes() == SHARED
        assert not (fleet.local / name).exists()
        assert _ledger_state(name) == (None, PEER)
    assert (fleet.local / "c.md").read_bytes() == SHARED
    assert _ledger_state("c.md") == (_sha(SHARED), None)
    rows = [r for r in pullhistory.read_records() if r.get("action") == "deleted"]
    assert [r["rel_path"] for r in rows] == ["a.md"]
    assert str(folder) in capture["output"][1]
    assert "deletion interrupted" in capture["output"][1]


def _setup_aliased_deletion(
    fleet, *, alias_has_tombstone=False, nested_alias=False, case_alias=False
):
    name = "nested/note.md" if nested_alias else "note.md"
    path = _put_local(fleet, name, SHARED, 1_600_000_000)
    alias_root = path.parent if nested_alias else fleet.local
    if case_alias:
        differently_cased = alias_root.with_name(alias_root.name.swapcase())
        if not differently_cased.exists() or not differently_cased.samefile(alias_root):
            pytest.skip("requires a case-insensitive source filesystem")
        assert differently_cased.resolve() != alias_root.resolve()
        alias_root = differently_cased
    alias = {"name": "alias", "type": "generic", "path": str(alias_root), "include_dirs": ["."]}
    sources = fleet.config["sync"]["sources"]
    sources.append(alias)
    prior = manifest.build_manifest_v2(LOCAL, "Local A", sources)
    core.publish_anchors(prior, sources, {})
    ledger, _ = core.load_ledger(None)
    ledger.settle(PEER, NOTES, name, _sha(SHARED))
    ledger.settle(PEER, "alias", "note.md", _sha(SHARED))
    core.record_published(ledger, prior)
    assert core.commit_ledger(ledger) is None
    tombstones = {f"{NOTES}:{name}": _tombstone(SHARED)}
    if alias_has_tombstone:
        tombstones["alias:note.md"] = _tombstone(SHARED)
    _seed(fleet, {}, tombstones=tombstones)
    return name, path, sources, prior


@pytest.mark.parametrize("alias_has_tombstone", [False, True])
@pytest.mark.parametrize("nested_alias", [False, True])
@pytest.mark.parametrize("selected_source", [None, NOTES])
@pytest.mark.parametrize("case_alias", [False, True], ids=["same-case", "case-alias"])
def test_followed_deletion_cannot_remint_through_an_aliased_source(
    fleet, monkeypatch, alias_has_tombstone, nested_alias, selected_source, case_alias
):
    name, path, sources, prior = _setup_aliased_deletion(
        fleet,
        alias_has_tombstone=alias_has_tombstone,
        nested_alias=nested_alias,
        case_alias=case_alias,
    )

    result, _, _ = _run(fleet, monkeypatch, "quiet", source_filter=selected_source)

    assert not path.exists()
    ledger, error = core.load_ledger(None)
    assert error is None
    # Exercise the next push's actual mint pipeline against its unchanged anchors.
    current = manifest.build_manifest_v2(LOCAL, "Local A", sources)
    guard = core.compute_mint_guard(current, prior, sources, ledger)
    assert (
        manifest.generate_tombstones(
            current, prior, LOCAL, skip_mint=guard.skip, deleted_hashes=guard.deleted_hashes
        )
        == {}
    )
    assert result.total_deleted == 1 and result.total_failed == 0
    copies = _copies()
    assert len(copies) == 1 and list(copies.values()) == [SHARED]
    assert set(copies) <= {f"{NOTES}/{name}", "alias/note.md"}
    for source, rel in ((NOTES, name), ("alias", "note.md")):
        assert ledger.base(PEER, source, rel) is None
        assert ledger.newest_stamp(source, rel) == PEER


@pytest.mark.parametrize("names", [("a.md", "b.md"), ("one/note.md", "two/note.md")])
def test_deletion_preserves_distinct_hardlink_directory_entries(fleet, monkeypatch, names):
    first = _put_local(fleet, names[0], SHARED, 1_600_000_000)
    second = fleet.local / names[1]
    second.parent.mkdir(parents=True, exist_ok=True)
    os.link(first, second)
    assert first.samefile(second)
    for name in names:
        _record((name, SHARED))
    _seed(fleet, {}, tombstones={f"{NOTES}:{name}": _tombstone(SHARED) for name in names})

    result, _, _ = _run(fleet, monkeypatch, "quiet")

    assert result.total_deleted == 2 and result.total_failed == 0
    assert not first.exists() and not second.exists()
    assert _copies() == {f"{NOTES}/{name}": SHARED for name in names}
    for name in names:
        assert _ledger_state(name) == (None, PEER)


@pytest.mark.parametrize("failure", ["refused", "restored", "interrupt-before", "interrupt-after"])
def test_aliased_deletion_failure_restores_all_bases_unless_the_inode_moved(
    fleet, monkeypatch, failure
):
    name, path, _, _ = _setup_aliased_deletion(fleet)
    devices.register_device(fleet.backend, "cccccccc", "Peer C")
    ledger, _ = core.load_ledger(None)
    # The alias can have a different history from the tombstone's source.
    ledger.settle("cccccccc", "alias", "note.md", _sha(b"another peer's old version"), hold=False)
    assert core.commit_ledger(ledger) is None
    before = dict(ledger.bases)
    real_move = pullflow._move_into_copy

    def fail_move(local, copy_path, sha):
        if failure == "refused":
            raise OSError(errno.EACCES, "refused")
        if failure == "interrupt-before":
            raise KeyboardInterrupt("before move")
        if failure == "restored":
            local.write_bytes(b"an agent's edit")
        result = real_move(local, copy_path, sha)
        if failure == "interrupt-after":
            raise KeyboardInterrupt("after move")
        return result

    monkeypatch.setattr(pullflow, "_move_into_copy", fail_move)
    if failure.startswith("interrupt-"):
        with pytest.raises(KeyboardInterrupt):
            _run(fleet, monkeypatch, "quiet")
    else:
        result, _, _ = _run(fleet, monkeypatch, "quiet")
        assert result.total_deleted == 0 and result.total_failed == 1
    after, error = core.load_ledger(None)
    assert error is None
    if failure == "interrupt-after":
        assert not path.exists()
        assert list(_copies().values()) == [SHARED]
        for source, rel in ((NOTES, name), ("alias", "note.md")):
            assert after.newest_stamp(source, rel) == PEER
        assert after.bases == {}
    else:
        assert path.read_bytes() == (b"an agent's edit" if failure == "restored" else SHARED)
        assert after.bases == before
        for source, rel in ((NOTES, name), ("alias", "note.md")):
            assert after.newest_stamp(source, rel) is None


def test_aliased_deletion_stamp_survives_an_incoming_sidecar_flush_failure(fleet, monkeypatch):
    name, path, sources, prior = _setup_aliased_deletion(fleet)
    remote_bytes = b"a different peer version"
    remote_sha = _sha(remote_bytes)
    peer = {
        "device_id": PEER,
        "sources": {
            NOTES: {"files": {}},
            "alias": {
                "files": {
                    "note.md": {
                        "sha256": remote_sha,
                        "size": len(remote_bytes),
                        "mtime": "2026-01-01T00:00:00+00:00",
                    }
                }
            },
        },
        "tombstones": {f"{NOTES}:{name}": _tombstone(SHARED)},
    }
    fleet.backend.put(
        blob_key(PEER, remote_sha), crypto.encrypt(remote_bytes, PASSPHRASE, MEMORY_KB)
    )
    fleet.backend.put(
        manifest_key(PEER), crypto.encrypt(manifest.serialize_manifest(peer), PASSPHRASE, MEMORY_KB)
    )
    ledger, _ = core.load_ledger(None)
    ledger.settle(PEER, "alias", "note.md", _sha(b"old base"), hold=False)
    assert core.commit_ledger(ledger) is None
    real_fsync = pullflow.fsutil.fsync_file

    def fail_sidecar_flush(file):
        if manifest.CONFLICT_INFIX in file.name:
            raise OSError(errno.EIO, "injected sidecar flush failure")
        return real_fsync(file)

    monkeypatch.setattr(pullflow.fsutil, "fsync_file", fail_sidecar_flush)
    result, _, _ = _run(fleet, monkeypatch, "quiet")
    assert result.total_conflicted == result.total_deleted == 1
    assert result.durability_fsync_failures == 1
    assert not path.exists()
    after, _ = core.load_ledger(None)
    assert after.newest_stamp("alias", "note.md") == PEER
    current = manifest.build_manifest_v2(LOCAL, "Local A", sources)
    guard = core.compute_mint_guard(current, prior, sources, after)
    assert (
        manifest.generate_tombstones(
            current, prior, LOCAL, skip_mint=guard.skip, deleted_hashes=guard.deleted_hashes
        )
        == {}
    )


# ── Track 108A: MEMORY.md index dedup and post-pull dangling-link tidy ──


def test_a_line_for_a_file_deleted_here_does_not_return_from_the_peer(fleet, monkeypatch):
    base_index = b"- [Gone](gone.md)\n- [Keep](keep.md)\n"
    local_index = b"- [Keep](keep.md)\n- [Local](local.md)\n"
    peer_index = b"- [Gone](gone.md)\n- [Keep](keep.md)\n- [Peer](peer.md)\n"
    _seed(
        fleet,
        {
            "memory/MEMORY.md": peer_index,
            "memory/keep.md": b"keep\n",
            "memory/peer.md": b"peer\n",
        },
    )
    _put_local(fleet, "memory/MEMORY.md", local_index, 1_600_000_000)
    _put_local(fleet, "memory/keep.md", b"keep\n", 1_600_000_000)
    _put_local(fleet, "memory/local.md", b"local\n", 1_600_000_000)
    _record(("memory/MEMORY.md", base_index), ("memory/keep.md", b"keep\n"))

    result, _, (_, err) = _run(fleet, monkeypatch, "attended", verbose=True)

    assert result.total_merged == 1
    assert err == ""
    assert not (fleet.local / "memory/gone.md").exists()
    assert sorted(p.name for p in (fleet.local / "memory").iterdir()) == [
        "MEMORY.md",
        "keep.md",
        "local.md",
        "peer.md",
    ]
    assert (fleet.local / "memory/MEMORY.md").read_bytes() == (
        b"- [Keep](keep.md)\n- [Local](local.md)\n- [Peer](peer.md)\n"
    )


def test_a_line_for_a_file_this_pull_deletes_is_dropped_after_the_deletion(fleet, monkeypatch):
    gone_bytes = b"to be deleted\n"
    base_index = b"- [Gone](gone.md)\n- [Keep](keep.md)\n"
    local_index = b"- [Gone](gone.md)\n- [Keep](keep.md)\n- [Local](local.md)\n"
    peer_index = b"- [Keep](keep.md)\n"
    _seed(
        fleet,
        {
            "memory/MEMORY.md": peer_index,
            "memory/keep.md": b"keep\n",
        },
        tombstones={"notes:memory/gone.md": _tombstone(gone_bytes)},
    )
    _put_local(fleet, "memory/MEMORY.md", local_index, 1_600_000_000)
    _put_local(fleet, "memory/gone.md", gone_bytes, 1_600_000_000)
    _put_local(fleet, "memory/keep.md", b"keep\n", 1_600_000_000)
    _put_local(fleet, "memory/local.md", b"local\n", 1_600_000_000)
    _record(
        ("memory/MEMORY.md", base_index),
        ("memory/gone.md", gone_bytes),
        ("memory/keep.md", b"keep\n"),
    )

    result, _, _ = _run(fleet, monkeypatch, "attended")

    assert result.total_deleted == 1
    assert result.total_merged == 0
    assert _copies() == {"notes/memory/gone.md": gone_bytes}
    assert not (fleet.local / "memory/gone.md").exists()
    assert (fleet.local / "memory/MEMORY.md").read_bytes() == (
        b"- [Keep](keep.md)\n- [Local](local.md)\n"
    )


def test_two_lines_for_one_file_keep_the_local_one(fleet, monkeypatch):
    base_index = b"- [Base](x.md) -- base\n"
    local_index = b"- [Zebra](x.md) -- local wording\n"
    peer_index = b"- [Alpha](x.md) -- peer wording\n"
    _seed(
        fleet,
        {
            "memory/MEMORY.md": peer_index,
            "memory/x.md": b"content\n",
        },
    )
    _put_local(fleet, "memory/MEMORY.md", local_index, 1_600_000_000)
    _put_local(fleet, "memory/x.md", b"content\n", 1_600_000_000)
    _record(("memory/MEMORY.md", base_index), ("memory/x.md", b"content\n"))

    result, _, _ = _run(fleet, monkeypatch, "attended")

    assert result.total_merged == 0
    assert (fleet.local / "memory/MEMORY.md").read_bytes() == local_index


def test_a_line_with_no_link_is_kept(fleet, monkeypatch):
    base_index = b"# Memory\n"
    local_index = b"# Memory\n- plain local bullet\n- [Local web](https://example.com/a.md)\n"
    peer_index = b"# Memory\n- plain peer bullet\n- [Peer web](https://example.com/b.md)\n"
    _seed(fleet, {"memory/MEMORY.md": peer_index})
    _put_local(fleet, "memory/MEMORY.md", local_index, 1_600_000_000)
    _record(("memory/MEMORY.md", base_index))

    result, _, _ = _run(fleet, monkeypatch, "attended")

    assert result.total_merged == 1
    assert (fleet.local / "memory/MEMORY.md").read_bytes() == (
        b"# Memory\n"
        b"- [Local web](https://example.com/a.md)\n"
        b"- [Peer web](https://example.com/b.md)\n"
        b"- plain local bullet\n"
        b"- plain peer bullet\n"
    )


def test_a_link_to_a_file_the_same_pull_adds_is_kept(fleet, monkeypatch):
    base_index = b"- [Local](local.md)\n"
    local_index = b"- [Local](local.md)\n- [Extra](extra.md)\n"
    peer_index = b"- [Added](added.md)\n- [Local](local.md)\n"
    _seed(
        fleet,
        {
            "memory/MEMORY.md": peer_index,
            "memory/added.md": b"added by peer\n",
            "memory/local.md": b"local\n",
        },
    )
    _put_local(fleet, "memory/MEMORY.md", local_index, 1_600_000_000)
    _put_local(fleet, "memory/local.md", b"local\n", 1_600_000_000)
    _put_local(fleet, "memory/extra.md", b"extra\n", 1_600_000_000)
    _record(("memory/MEMORY.md", base_index), ("memory/local.md", b"local\n"))

    result, _, _ = _run(fleet, monkeypatch, "attended")

    assert result.total_written == 1
    assert result.total_merged == 1
    assert (fleet.local / "memory/added.md").read_bytes() == b"added by peer\n"
    assert (fleet.local / "memory/MEMORY.md").read_bytes() == (
        b"- [Added](added.md)\n- [Extra](extra.md)\n- [Local](local.md)\n"
    )


@pytest.mark.parametrize("pending_kind", ("waiting", "failed"))
def test_a_line_for_a_file_this_pull_left_waiting_or_failed_is_kept(
    fleet, syncthing, monkeypatch, pending_kind
):
    base_index = b"- [Shared](shared.md)\n"
    local_index = b"- [Local](local.md)\n- [Shared](shared.md)\n"
    peer_index = b"- [Gone](gone.md)\n- [Pending](pending.md)\n- [Shared](shared.md)\n"
    peer, _ = _seed(
        fleet,
        {
            "memory/MEMORY.md": peer_index,
            "memory/pending.md": b"arriving later\n",
            "memory/shared.md": b"shared\n",
        },
    )
    _put_local(fleet, "memory/MEMORY.md", local_index, 1_600_000_000)
    _put_local(fleet, "memory/local.md", b"local\n", 1_600_000_000)
    _put_local(fleet, "memory/shared.md", b"shared\n", 1_600_000_000)
    _record(("memory/MEMORY.md", base_index), ("memory/shared.md", b"shared\n"))
    if pending_kind == "waiting":
        syncthing.hide(_blob(peer, "memory/pending.md"))
    else:
        fleet.backend.put(_blob(peer, "memory/pending.md"), b"corrupt")

    result, _, _ = _run(fleet, monkeypatch, "quiet")

    assert not (fleet.local / "memory/pending.md").exists()
    if pending_kind == "waiting":
        assert result.total_waiting == 1 and result.total_failed == 0
    else:
        assert result.total_failed == 1 and result.total_waiting == 0
    assert (fleet.local / "memory/MEMORY.md").read_bytes() == (
        b"- [Local](local.md)\n- [Pending](pending.md)\n- [Shared](shared.md)\n"
    )


@pytest.mark.parametrize("pending_kind", ("waiting", "failed"))
@pytest.mark.parametrize("case_insensitive", (True, False))
def test_pending_memory_link_respects_filesystem_case(
    fleet, syncthing, monkeypatch, pending_kind, case_insensitive
):
    base_index = b"- [Shared](shared.md)\n"
    local_index = b"- [Local](local.md)\n- [Shared](shared.md)\n"
    peer_index = b"- [Gone](gone.md)\n- [Note](foo.md)\n- [Shared](shared.md)\n"
    peer, _ = _seed(
        fleet,
        {
            "memory/MEMORY.md": peer_index,
            "memory/Foo.md": b"arriving later\n",
            "memory/shared.md": b"shared\n",
        },
    )
    index_path = _put_local(fleet, "memory/MEMORY.md", local_index, 1_600_000_000)
    _put_local(fleet, "memory/local.md", b"local\n", 1_600_000_000)
    _put_local(fleet, "memory/shared.md", b"shared\n", 1_600_000_000)
    _record(("memory/MEMORY.md", base_index), ("memory/shared.md", b"shared\n"))
    monkeypatch.setattr(pullflow, "_detect_case_insensitive_fs", lambda path: case_insensitive)
    if pending_kind == "waiting":
        syncthing.hide(_blob(peer, "memory/Foo.md"))
    else:
        fleet.backend.put(_blob(peer, "memory/Foo.md"), b"corrupt")

    result, _, _ = _run(fleet, monkeypatch, "quiet")

    assert not (fleet.local / "memory/Foo.md").exists()
    assert not (fleet.local / "memory/foo.md").exists()
    assert result.total_merged == 1
    assert result.total_waiting == (pending_kind == "waiting")
    assert result.total_failed == (pending_kind == "failed")
    expected = b"- [Local](local.md)\n"
    if case_insensitive:
        expected += b"- [Note](foo.md)\n"
    expected += b"- [Shared](shared.md)\n"
    assert index_path.read_bytes() == expected


def test_an_index_changed_after_its_merge_is_left_alone(fleet, monkeypatch):
    base_index = b"- [Keep](keep.md)\n"
    local_index = b"- [Keep](keep.md)\n- [Local](local.md)\n"
    peer_index = b"- [Gone](gone.md)\n- [Keep](keep.md)\n"
    _seed(fleet, {"memory/MEMORY.md": peer_index, "memory/keep.md": b"keep\n"})
    index_path = _put_local(fleet, "memory/MEMORY.md", local_index, 1_600_000_000)
    _put_local(fleet, "memory/keep.md", b"keep\n", 1_600_000_000)
    _put_local(fleet, "memory/local.md", b"local\n", 1_600_000_000)
    _record(("memory/MEMORY.md", base_index), ("memory/keep.md", b"keep\n"))

    real_candidates = pullflow._deletion_candidates
    concurrent_bytes = b"- [Concurrent](gone.md) -- written mid-pull\n"

    def rewrite_on_real_pass(*args, **kwargs):
        if "blocked" in kwargs:
            index_path.write_bytes(concurrent_bytes)
        return real_candidates(*args, **kwargs)

    monkeypatch.setattr(pullflow, "_deletion_candidates", rewrite_on_real_pass)

    result, _, _ = _run(fleet, monkeypatch, "attended")

    assert result.total_merged == 1
    assert index_path.read_bytes() == concurrent_bytes


@pytest.mark.parametrize("index_kind", ("missing", "directory", "symlink"))
def test_tidy_preserves_missing_or_nonregular_index(tmp_path, index_kind):
    merged = b"- [Gone](gone.md)\n- plain memory note\n"
    index_path = tmp_path / "MEMORY.md"
    linked_file = tmp_path / "other-index.md"
    linked_file.write_bytes(merged)
    if index_kind == "directory":
        index_path.mkdir()
    elif index_kind == "symlink":
        index_path.symlink_to(linked_file)

    pullflow._tidy_line_indexes({index_path: merged})

    if index_kind == "missing":
        assert not index_path.exists()
    elif index_kind == "directory":
        assert index_path.is_dir()
        assert list(index_path.iterdir()) == []
    else:
        assert index_path.is_symlink()
        assert index_path.readlink() == linked_file
    assert linked_file.read_bytes() == merged


@pytest.mark.parametrize("disappearance", ("before-read", "before-recheck"))
def test_tidy_does_not_recreate_index_that_disappears(tmp_path, monkeypatch, disappearance):
    # Generated by /ship test coverage audit (Track 108A).
    # Value: protects=an index disappearing during tidy stays absent without a failure notice;
    # fails_when=tidy recreates an index removed after its initial stat or before its recheck;
    # why_new=existing guards cover initial absence and inode replacement, not mid-pass removal;
    # seam=none; no_claim=all filesystem race schedules (removal injected at real I/O boundaries).
    merged = b"- [Gone](gone.md)\n- plain memory note\n"
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    index_path = memory_dir / "MEMORY.md"
    index_path.write_bytes(merged)
    real_read = Path.read_bytes
    real_lstat = os.lstat
    removed = []

    def read_after_removal(path):
        if path == index_path and disappearance == "before-read":
            path.unlink()
            removed.append(disappearance)
        return real_read(path)

    def check_after_removal(path, *args, **kwargs):
        if Path(path) == memory_dir / "gone.md" and disappearance == "before-recheck":
            index_path.unlink()
            removed.append(disappearance)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read_after_removal)
    monkeypatch.setattr(pullflow.os, "lstat", check_after_removal)
    notices = []
    monkeypatch.setattr(pullflow.core, "_print_apply_warning", notices.append)

    pullflow._tidy_line_indexes({index_path: merged})

    assert removed == [disappearance]
    assert not index_path.exists()
    assert list(memory_dir.iterdir()) == []
    assert notices == []


def test_tidy_preserves_index_replaced_during_target_presence_check(tmp_path, monkeypatch):
    merged = b"- [Gone](gone.md)\n- plain memory note\n"
    index_path = tmp_path / "MEMORY.md"
    index_path.write_bytes(merged)
    replacement = tmp_path / "replacement.md"
    replacement.write_bytes(merged)
    real_lstat = os.lstat
    replaced = False

    def replace_during_presence(path, *args, **kwargs):
        nonlocal replaced
        if Path(path) == tmp_path / "gone.md":
            replacement.replace(index_path)
            replaced = True
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(pullflow.os, "lstat", replace_during_presence)

    pullflow._tidy_line_indexes({index_path: merged})

    assert replaced
    assert not replacement.exists()
    assert index_path.read_bytes() == merged


@pytest.mark.parametrize("presence", ("dangling-symlink", "permission-error", "value-error"))
def test_tidy_keeps_link_when_target_presence_is_uncertain(tmp_path, monkeypatch, presence):
    merged = b"- [Gone](gone.md)\n- [Retained](retained.md)\n- plain memory note\n"
    index_path = tmp_path / "MEMORY.md"
    index_path.write_bytes(merged)
    retained = tmp_path / "retained.md"
    real_lstat = os.lstat
    probes = []
    if presence == "dangling-symlink":
        retained.symlink_to("not-here.md")

    def uncertain_presence(path, *args, **kwargs):
        if Path(path) == retained:
            probes.append(path)
            if presence == "permission-error":
                raise PermissionError("injected unreadable target")
            if presence == "value-error":
                raise ValueError("injected invalid target")
        return real_lstat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(pullflow.os, "lstat", uncertain_presence)
        pullflow._tidy_line_indexes({index_path: merged})

    assert probes == [retained]
    assert index_path.read_bytes() == b"- [Retained](retained.md)\n- plain memory note\n"
    assert not (tmp_path / "gone.md").exists()
    assert not (tmp_path / "not-here.md").exists()
    if presence == "dangling-symlink":
        assert retained.is_symlink()
        assert retained.readlink() == Path("not-here.md")
    else:
        assert not retained.exists()


def test_a_failed_tidy_keeps_the_merged_index_and_prints_one_notice(fleet, monkeypatch):
    base_index = b"- [Keep](keep.md)\n"
    local_index = b"- [Keep](keep.md)\n- [Local](local.md)\n"
    peer_index = b"- [Gone](gone.md)\n- [Keep](keep.md)\n"
    _seed(fleet, {"memory/MEMORY.md": peer_index, "memory/keep.md": b"keep\n"})
    index_path = _put_local(fleet, "memory/MEMORY.md", local_index, 1_600_000_000)
    _put_local(fleet, "memory/keep.md", b"keep\n", 1_600_000_000)
    _put_local(fleet, "memory/local.md", b"local\n", 1_600_000_000)
    _record(("memory/MEMORY.md", base_index), ("memory/keep.md", b"keep\n"))

    real_write = pullflow.fsutil.atomic_write_bytes

    def fail_tidy_write(path, data, *, mode=None, fsync=False):
        if Path(path).name == "MEMORY.md" and fsync:
            raise pullflow.StorageError("injected tidy disk full")
        return real_write(path, data, mode=mode, fsync=fsync)

    monkeypatch.setattr(pullflow.fsutil, "atomic_write_bytes", fail_tidy_write)

    result, _, (_, err) = _run(fleet, monkeypatch, "attended")

    assert result.total_merged == 1
    assert result.total_failed == 0
    assert index_path.read_bytes() == (
        b"- [Gone](gone.md)\n- [Keep](keep.md)\n- [Local](local.md)\n"
    )
    notice_lines = [line for line in err.splitlines() if line.startswith("mm: notice:")]
    assert len(notice_lines) == 1
    assert "index tidy failed (injected tidy disk full)" in notice_lines[0]
    assert "lines for missing memory files may remain." in notice_lines[0]


def test_a_dry_run_never_tidies_an_index(fleet, monkeypatch):
    base_index = b"- [Gone](gone.md)\n- [Keep](keep.md)\n"
    local_index = b"- [Keep](keep.md)\n- [Local](local.md)\n"
    peer_index = b"- [Gone](gone.md)\n- [Keep](keep.md)\n- [Peer](peer.md)\n"
    _seed(
        fleet,
        {
            "memory/MEMORY.md": peer_index,
            "memory/keep.md": b"keep\n",
            "memory/peer.md": b"peer\n",
        },
    )
    _put_local(fleet, "memory/MEMORY.md", local_index, 1_600_000_000)
    _put_local(fleet, "memory/keep.md", b"keep\n", 1_600_000_000)
    _put_local(fleet, "memory/local.md", b"local\n", 1_600_000_000)
    _record(("memory/MEMORY.md", base_index), ("memory/keep.md", b"keep\n"))
    before = _snapshot(fleet.local)

    result, _, _ = _run(fleet, monkeypatch, "attended", dry_run=True)

    assert result.total_applied == 0
    assert _snapshot(fleet.local) == before


# ── Track 92A: peer cursors recorded after pull ───────────────────────


def _expected_cursor(applied_manifest, seen_manifest, waiting):
    applied_hash = core._manifest_content_hash(applied_manifest)
    return statedb.CursorRow(
        pullflow._aware_timestamp_us(applied_manifest["timestamp"]),
        pullflow._aware_timestamp_us(seen_manifest["timestamp"]),
        waiting,
        applied_hash,
        applied_hash,
        core._manifest_content_hash(seen_manifest),
    )


def _cursors() -> dict[str, statedb.CursorRow]:
    with statedb.connect(read_only=True) as conn:
        return statedb.read_cursors(conn)


def test_canonical_manifest_with_surrogate_metadata_remains_readable(fleet, monkeypatch):
    peer, _ = _seed(fleet, {"note.md": b"peer content\n"})
    peer["device_name"] = "Peer \udcff"
    _republish(fleet, peer)

    fetched = core._fetch_remote_manifest(fleet.backend, PEER, PASSPHRASE, MEMORY_KB)
    assert fetched.is_ok
    assert fetched.manifest["device_name"] == peer["device_name"]
    result, _, _ = _run(fleet, monkeypatch, "attended")
    assert result.total_written == 1 and result.total_failed == 0
    assert (fleet.local / "note.md").read_bytes() == b"peer content\n"


def test_late_older_manifest_copy_invalidates_applied_then_cleanup_stays_applied(
    fleet, monkeypatch
):
    from mind_meld import statusflow

    first, _ = _seed(fleet, {"a.md": b"alpha\n"})
    _run(fleet, monkeypatch, "attended")

    def peer_state():
        fetched = core._fetch_remote_manifest(fleet.backend, PEER, PASSPHRASE, MEMORY_KB)
        states = statusflow._peer_states(
            [{"device_id": PEER, "device_name": "Peer B"}],
            LOCAL,
            {PEER: fetched},
            _cursors(),
        )
        return states[0], fetched

    assert peer_state()[0].state == "applied"
    older, _ = _seed(fleet, {"a.md": b"alpha\n", "late.md": b"late content\n"})
    older["timestamp"] = (
        datetime.fromisoformat(first["timestamp"]) - timedelta(hours=1)
    ).isoformat()
    _republish(fleet, first)
    canonical = fleet.backend.root / manifest_key(PEER)
    copy = canonical.with_name(f"manifest.json.{CONFLICT_STAMP}.enc")
    copy_bytes = crypto.encrypt(manifest.serialize_manifest(older), PASSPHRASE, MEMORY_KB)
    copy.write_bytes(copy_bytes)

    state, fetched = peer_state()
    assert fetched.manifest["timestamp"] == first["timestamp"]
    assert "late.md" in fetched.manifest["sources"]["notes"]["files"]
    assert not (fleet.local / "late.md").exists()
    assert state.state == "not-pulled"

    pulled, _, _ = _run(fleet, monkeypatch, "attended")
    assert pulled.total_written == 1
    assert (fleet.local / "late.md").read_bytes() == b"late content\n"
    assert not copy.exists()
    assert peer_state()[0].state == "applied"
    copy.write_bytes(copy_bytes)
    assert peer_state()[0].state == "applied"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("from_device", [None, PEER])
def test_a_complete_pull_records_the_peers_applied_manifest_time(
    fleet, monkeypatch, mode, from_device
):
    peer, _ = _seed(fleet, {"note.md": b"peer content\n"})
    expected_us = pullflow._aware_timestamp_us(peer["timestamp"])
    assert expected_us is not None

    result, _, _ = _run(fleet, monkeypatch, mode, from_device=from_device)

    assert result.total_written == 1 and result.total_failed == 0
    assert _cursors() == {PEER: _expected_cursor(peer, peer, 0)}


def test_waiting_files_record_the_count_and_keep_the_earlier_applied_time(
    fleet, syncthing, monkeypatch
):
    first, _ = _seed(fleet, {"a.md": b"alpha\n"})
    first_us = pullflow._aware_timestamp_us(first["timestamp"])
    assert first_us is not None
    _run(fleet, monkeypatch, "attended")
    assert _cursors() == {PEER: _expected_cursor(first, first, 0)}

    second, _ = _seed(fleet, {"a.md": b"alpha\n", "b.md": b"bravo\n", "c.md": b"charlie\n"})
    second["timestamp"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    _republish(fleet, second)
    second_us = pullflow._aware_timestamp_us(second["timestamp"])
    assert second_us is not None and second_us > first_us
    for name in ("b.md", "c.md"):
        syncthing.hide(_blob(second, name))

    result, _, _ = _run(fleet, monkeypatch, "attended")
    assert result.total_waiting == 2 and result.total_failed == 0
    assert _cursors() == {PEER: _expected_cursor(first, second, 2)}

    for name in ("b.md", "c.md"):
        syncthing.arrive(_blob(second, name))
    result, _, _ = _run(fleet, monkeypatch, "attended")
    assert result.total_written == 2 and result.total_waiting == 0
    assert _cursors() == {PEER: _expected_cursor(second, second, 0)}


def test_failed_files_leave_the_push_unapplied(fleet, monkeypatch):
    first, _ = _seed(fleet, {"a.md": b"alpha\n"})
    first_us = pullflow._aware_timestamp_us(first["timestamp"])
    assert first_us is not None
    _run(fleet, monkeypatch, "attended")

    second, _ = _seed(fleet, {"a.md": b"alpha\n", "bad.md": b"corrupt\n"})
    second["timestamp"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    _republish(fleet, second)
    second_us = pullflow._aware_timestamp_us(second["timestamp"])
    assert second_us is not None
    bad_blob = _blob(second, "bad.md")
    raw = fleet.backend.get(bad_blob)
    fleet.backend.put(bad_blob, raw[:-1] + bytes([raw[-1] ^ 1]))

    result, _, _ = _run(fleet, monkeypatch, "attended")
    assert result.total_failed == 1
    assert _cursors() == {PEER: _expected_cursor(first, second, 0)}


def test_dry_run_scoped_and_interrupted_pulls_record_nothing(fleet, monkeypatch):
    _seed(fleet, {"note.md": b"peer content\n"})

    # Dry run never creates state.db or records cursors.
    _run(fleet, monkeypatch, "attended", dry_run=True)
    assert not statedb.db_path().exists()

    # Source-filtered pull leaves cursors untouched.
    _run(fleet, monkeypatch, "attended", source_filter="notes")
    assert _cursors() == {}

    # Interrupted pull leaves cursors untouched.
    (fleet.local / "note.md").unlink()
    monkeypatch.setattr(
        pullflow,
        "_pull_one_source",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        _run(fleet, monkeypatch, "attended")
    assert _cursors() == {}


@pytest.mark.parametrize("mode", ("attended", "quiet"))
def test_a_cursor_write_failure_is_one_notice_and_never_fails_the_pull(fleet, monkeypatch, mode):
    _seed(fleet, {"note.md": b"peer content\n"})

    def fail_cursors(conn, rows):
        raise statedb.StateDBBusy("SQLITE_BUSY")

    monkeypatch.setattr(statedb, "commit_cursors", fail_cursors)

    result, _, (_, err) = _run(fleet, monkeypatch, mode)

    assert result.total_written == 1 and result.total_failed == 0
    assert result.maintenance_failures == 1
    assert (fleet.local / "note.md").read_bytes() == b"peer content\n"
    notices = [line for line in err.splitlines() if line.startswith("mm: notice:")]
    assert len(notices) == 1
    assert "peer push record not saved (busy);" in notices[0]
    assert "mm status may show an earlier pull." in notices[0]


# ── Track 130B: shared error, warning, and outcome style in pullflow ──


class TestDownloadAndApply:
    @pytest.mark.parametrize(
        "branch",
        ("bad-blob-key", "decrypt-failed", "escape-root", "content-check"),
    )
    @pytest.mark.parametrize("quiet", (False, True))
    def test_download_and_apply_per_file_error_branches(
        self, fleet, monkeypatch, capsys, branch, quiet
    ):
        rel = "note-[red]tag[/red]\x1b]52;c;ZGF0YQ==\x07\nextra.md"
        safe_rel = "note-[red]tag[/red]\\nextra.md"
        data = b"peer content\n"
        good_sha = _sha(data)
        enc = crypto.encrypt(data, PASSPHRASE, MEMORY_KB)
        fleet.backend.put(blob_key(PEER, good_sha), enc)

        if branch == "bad-blob-key":
            info = {"sha256": "../bad\x1b[31m\nsha", "size": len(data)}
        elif branch == "decrypt-failed":
            fleet.backend.put(blob_key(PEER, good_sha), enc[:-1] + bytes([enc[-1] ^ 1]))
            info = {"sha256": good_sha, "size": len(data)}
        elif branch == "escape-root":
            rel = "../escape-[red]tag[/red]\x1b[31m\n.md"
            safe_rel = "../escape-[red]tag[/red]\\n.md"
            info = {"sha256": good_sha, "size": len(data)}
        else:
            info = {"sha256": _sha(b"different content\n"), "size": len(data)}
            fleet.backend.put(blob_key(PEER, info["sha256"]), enc)

        reporter = pullflow._ApplyReporter(
            peer_id=PEER,
            device_name="Peer B",
            src_name=NOTES,
            outcomes=pullflow._empty_outcomes(),
            touched_parents=set(),
            ledger=pullplan.Ledger(),
            pending_inline_bumps={},
            line_indexes={},
        )
        scope_reset = core._SYNC_OUTPUT.set({"verbose": False, "diagnostics": [], "progress": None})
        try:
            pullflow._download_and_apply(
                backend=fleet.backend,
                base_path=fleet.local,
                to_download={rel: info},
                source_device_id=PEER,
                passphrase=PASSPHRASE,
                memory_kb=MEMORY_KB,
                verbose=True,
                quiet=quiet,
                reporter=reporter,
            )
        finally:
            core._SYNC_OUTPUT.reset(scope_reset)

        captured = capsys.readouterr()
        assert reporter.outcomes["failed"] == [rel]
        assert captured.out == ""
        if quiet:
            assert captured.err == ""
        else:
            lines = captured.err.splitlines()
            assert len(lines) == 1
            assert "\x1b" not in lines[0]
            if branch == "bad-blob-key":
                assert lines[0].startswith(
                    f"mm: error: bad blob key (local preserved): {safe_rel} \u2014 "
                )
            elif branch == "decrypt-failed":
                assert lines[0].startswith(f"mm: error: decrypt failed: {safe_rel} \u2014 ")
            elif branch == "escape-root":
                assert lines[0] == f"mm: error: rejected (would escape source root): {safe_rel}"
            else:
                assert lines[0] == (
                    f"mm: error: content check failed: {safe_rel} does not match the "
                    f"sending snapshot. Local file kept. see {errors.SNAPSHOT_FAILURES_URL}."
                )


@pytest.mark.parametrize(
    "outcome,label,expected_fix",
    [
        ("conflict", "conflict", "mm pull --conflict-mode keep-both"),
        ("may fail", "may fail (unreadable local destination)", "mm diag"),
    ],
)
@pytest.mark.parametrize("mode", MODES)
def test_preflight_conflicts_direct_fallback_and_non_duplication(
    capsys, outcome, label, expected_fix, mode
):
    pred = pullplan.PullPrediction(
        device_id=PEER,
        device_name="Peer [red]B[/red]\x1b[31m\n2",
        src_name="notes\n1",
        rel_path="sub/note.md",
        outcome=outcome,
        reason="" if outcome == "conflict" else "unreadable local destination",
    )
    state = {"verbose": False, "diagnostics": [], "progress": None} if mode == "attended" else None
    token = core._SYNC_OUTPUT.set(state)
    try:
        pullflow._print_preflight_conflicts([pred], quiet=(mode == "quiet"))
    finally:
        core._SYNC_OUTPUT.reset(token)

    captured = capsys.readouterr()
    assert captured.out == ""
    expected_warn = f"mm: warning: {label} notes\\n1/sub/note.md (from Peer [red]B[/red]\\n2)"
    err_lines = captured.err.splitlines()
    if mode in ("attended", "quiet"):
        assert err_lines == [expected_warn]
    else:
        assert err_lines == [
            expected_warn,
            "mm: error: Pull refused: 1 file(s) would conflict or may fail.",
            f"Run: {expected_fix}",
        ]


def test_verbose_deletion_prints_shared_outcome_line(fleet, monkeypatch):
    _setup_deletion(fleet, "note.md")
    result, _, (out, err) = _run(fleet, monkeypatch, "attended", verbose=True)
    assert result.total_deleted == 1
    assert err == ""
    assert "Pull: deleted note.md (a peer deleted it).\n" in out
    assert "[red]" not in out


@pytest.mark.parametrize(
    "kind",
    ("own_vanished", "newer_version", "corrupt_peer", "unknown_source", "fsync_warning"),
)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("dry_run", (False, True))
def test_summary_data_at_risk_warnings_across_modes_and_dry_run(capsys, kind, mode, dry_run):
    hostile = "bad[red]tag[/red]\x1b]52;c;ZGF0YQ==\x07\nline2"
    clean = "bad[red]tag[/red]\\nline2"
    corrupt_peers: list[pullflow._CorruptPeer] = []
    unknown_sources: list[pullflow._UnknownSourceWarning] = []
    fsync_warnings: list[pullflow._FsyncWarning] = []

    if kind == "own_vanished":
        corrupt_peers.append(
            pullflow._CorruptPeer(device_name="Self", device_id=hostile, own_vanished=True)
        )
        expected = (
            f"mm: warning: this device's manifest ({clean}) vanished during the read - "
            "its deletion records were not applied, so files deleted on this Mac "
            "may have come back from peers. Check them before the next mm push."
        )
    elif kind == "newer_version":
        corrupt_peers.append(
            pullflow._CorruptPeer(
                device_name=hostile,
                device_id="dev-2",
                newer_version=99,
            )
        )
        expected = "mm: warning: " + core._newer_format_message(99, f"manifest for {clean} (dev-2)")
    elif kind == "corrupt_peer":
        corrupt_peers.append(pullflow._CorruptPeer(device_name=hostile, device_id="dev-2"))
        expected = (
            f"mm: warning: manifest for device {clean} (dev-2) "
            "is corrupt - skipping pull from this device."
        )
    elif kind == "unknown_source":
        unknown_sources.append(
            pullflow._UnknownSourceWarning(device_name="Peer B", src_name=hostile)
        )
        expected = (
            f"mm: warning: skipping unknown source '{clean}' from Peer B - not configured locally"
        )
    else:
        fsync_warnings.append(
            pullflow._FsyncWarning(parent_dir=Path(f"/tmp/{hostile}"), error="EIO\x1b[31m\nbad")
        )
        expected = f"mm: warning: durability fsync failed on /tmp/{clean} \u2014 EIO\\nbad"

    state = {"verbose": False, "diagnostics": [], "progress": None} if mode == "attended" else None
    token = core._SYNC_OUTPUT.set(state)
    try:
        pullflow._print_pull_summary(
            pullflow.PullResult(),
            corrupt_peers,
            unknown_sources,
            fsync_warnings,
            [],
            quiet=(mode == "quiet"),
            verbose=False,
            dry_run=dry_run,
        )
    finally:
        core._SYNC_OUTPUT.reset(token)

    captured = capsys.readouterr()
    if mode != "direct" or dry_run:
        assert captured.out == ""
    else:
        assert "warning" not in captured.out.lower()
    err_lines = captured.err.splitlines()
    assert err_lines == [expected]
    assert "\x1b" not in captured.err


@pytest.mark.parametrize("mode", ("attended", "quiet"))
def test_bookkeeping_and_manifest_cleanup_warnings_use_shared_stderr_helper(
    fleet, monkeypatch, mode
):
    fleet.config["sync"]["sources"][0]["type"] = "claude"
    peer, _ = _seed(fleet, {"projects/demo/memory/note.md": b"peer content\n"})
    manifest_path = manifest_key(PEER)
    copy_path = manifest_path.replace("manifest.json.enc", f"manifest.json.{CONFLICT_STAMP}.enc")
    fleet.backend.put(copy_path, fleet.backend.get(manifest_path))

    monkeypatch.setattr(
        pullflow,
        "write_sync_log",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("log boom\x1b[31m\n2")),
    )
    monkeypatch.setattr(
        core,
        "_cleanup_conflict_copies",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("cleanup boom\x1b[31m\n2")),
    )

    _, summaries, (out, err) = _run(fleet, monkeypatch, mode)
    assert summaries == [("", "")]
    assert "warning" not in out.lower()
    err_lines = err.splitlines()
    assert "mm: warning: sync log write failed: log boom\\n2" in err_lines
    assert (
        "mm: warning: manifest conflict-copy cleanup failed for Peer B: cleanup boom\\n2"
        in err_lines
    )


def test_cli_autopull_surfaces_data_at_risk_warnings_on_stderr(fleet, monkeypatch):
    from typer.testing import CliRunner

    from mind_meld import cli
    from mind_meld import config as config_mod

    cfg_path = fleet.root / "config.toml"
    config_mod.save_config(fleet.config, cfg_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

    peer, _ = _seed(fleet, {"note.md": b"peer content\n"})
    peer["sources"]["mystery-src"] = {
        "type": "generic",
        "files": {
            "secret.md": {
                "sha256": _sha(b"peer content\n"),
                "size": 13,
                "mtime": datetime.now(timezone.utc).isoformat(),
            }
        },
    }
    _republish(fleet, peer)

    devices.register_device(fleet.backend, THIRD, "Broken Peer")
    devices.update_last_seen(fleet.backend, THIRD)
    fleet.backend.put(manifest_key(THIRD), b"not-valid-ciphertext")

    res = CliRunner().invoke(cli.app, ["autopull"])
    assert res.exit_code == 0, (res.stdout, res.stderr)
    assert res.stdout == ""
    err_lines = res.stderr.splitlines()
    assert (
        "mm: warning: manifest for device Broken Peer (cccccccc) "
        "is corrupt - skipping pull from this device." in err_lines
    )
    assert (
        "mm: warning: skipping unknown source 'mystery-src' from Peer B - "
        "not configured locally" in err_lines
    )
    crumb = cli._read_autorun_breadcrumbs()["pull"]
    assert crumb["outcome"] == "degraded"
    assert "1 corrupt peer manifest(s)" in crumb["detail"]
    assert "1 unknown source(s)" in crumb["detail"]
