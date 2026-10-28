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

import io
import os
import re
import subprocess
import sys
from contextlib import redirect_stderr
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import typer
from rich.console import Console

from mind_meld import core, crypto, devices, manifest, pullflow, pullhistory, sidecar, statedb
from mind_meld.storage.keys import blob_key, device_key, manifest_key
from mind_meld.storage.local import LocalBackend

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
@pytest.mark.parametrize("failure", ("missing", "corrupt"))
def test_bad_blob_failure_lines_and_counts(fleet, monkeypatch, mode, failure):
    peer, _ = _seed(fleet, {"note.md": b"peer content\n"})
    key = blob_key(PEER, peer["sources"]["notes"]["files"]["note.md"]["sha256"])
    if failure == "missing":
        fleet.backend.delete(key)
    else:
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
            f"blob missing: {key}"
            if failure == "missing"
            else (
                "decrypt failed: note.md — decrypt: GCM tag mismatch — wrong passphrase, "
                "wrong root_salt in mm-crypto-init, or corrupt blob. "
                "Verify mm-crypto-init integrity if other decrypts also fail."
            )
        )
        assert cause in " ".join(output[0].split())
        assert output[1] == ""
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
    warning = "mm: conflict notes/note.md (from Peer B)\n"
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
