"""PullResult, iter_source_diffs, and autopull degradation breadcrumbs.

Pins the unit semantics of `iter_source_diffs` (source_filter, skip_unchanged),
the four `PullResult` degradation counters, the autopull "degraded" breadcrumb
outcome (fires on fsync / corrupt-peer / unknown-source / failed-file), GC
malformed-sha safety, and autopull's typer.Exit handling for fleet-version
refusals. Originally landed as "Track 1C" (v0.8.6); kept as steady-state
coverage for these stable surfaces.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from mind_meld.cli import (
    PullResult,
    app,
    iter_source_diffs,
)
from mind_meld.crypto import bootstrap_crypto_init
from mind_meld.devices import register_device
from mind_meld.storage.local import LocalBackend

# Shared CLI-integration helpers live in tests/conftest.py.
from tests.conftest import (
    MEMORY_KB,
    PASSPHRASE,
    _make_config,
    _populate_claude,
    _redirect_lock,
    _redirect_sidecar,
    _setup_real_config,
)

runner = CliRunner()


# ─── iter_source_diffs unit tests ────────────────────────────────────────


def _fake_manifest(sources: dict[str, dict[str, dict]]) -> dict:
    """Build a minimal v2-shaped local manifest for iter_source_diffs tests."""
    return {
        "sources": {
            name: {"base_path": f"/fake/{name}", "files": files} for name, files in sources.items()
        }
    }


def _fake_remote(sources: dict[str, dict[str, dict]]) -> dict:
    """Build the `remote_sources` arg shape (what iter_source_diffs expects)."""
    return {name: {"files": files} for name, files in sources.items()}


def _fi(sha: str, size: int = 10) -> dict:
    """Compact file-info record."""
    return {"sha256": sha, "size": size, "mtime": "2026-04-24T00:00:00Z"}


class TestIterSourceDiffs:
    def test_yields_all_sources_when_no_filter(self):
        local = _fake_manifest(
            {
                "claude": {"a.md": _fi("aa")},
                "gstack": {"b.md": _fi("bb")},
            }
        )
        remote = _fake_remote(
            {
                "claude": {"a.md": _fi("aa")},
                "gstack": {},
            }
        )
        out = list(iter_source_diffs(local, remote))
        names = [t[0] for t in out]
        assert names == ["claude", "gstack"]

    def test_source_filter_narrows_to_one(self):
        local = _fake_manifest(
            {
                "claude": {"a.md": _fi("aa")},
                "gstack": {"b.md": _fi("bb")},
            }
        )
        out = list(iter_source_diffs(local, {}, source_filter="claude"))
        assert [t[0] for t in out] == ["claude"]

    def test_unknown_source_filter_yields_nothing(self):
        local = _fake_manifest({"claude": {"a.md": _fi("aa")}})
        out = list(iter_source_diffs(local, {}, source_filter="nonexistent"))
        assert out == []

    def test_missing_remote_src_defaults_to_empty_files(self):
        """Source present locally but absent from remote: diff treats remote as {}."""
        local = _fake_manifest({"claude": {"a.md": _fi("aa")}})
        out = list(iter_source_diffs(local, {}))
        assert len(out) == 1
        _name, _src_data, remote_src, diff = out[0]
        assert remote_src == {"files": {}}
        assert "a.md" in diff.new

    def test_skip_unchanged_drops_in_sync_sources(self):
        """skip_unchanged=True: filter out sources where diff.has_changes is False."""
        local = _fake_manifest(
            {
                "claude": {"a.md": _fi("aa")},
                "gstack": {"b.md": _fi("bb")},
            }
        )
        remote = _fake_remote(
            {
                "claude": {"a.md": _fi("aa")},  # in sync → should be skipped
                "gstack": {"b.md": _fi("changed")},  # diverged → should yield
            }
        )
        out = list(iter_source_diffs(local, remote, skip_unchanged=True))
        assert [t[0] for t in out] == ["gstack"]

    def test_skip_unchanged_false_keeps_in_sync_sources(self):
        local = _fake_manifest({"claude": {"a.md": _fi("aa")}})
        remote = _fake_remote({"claude": {"a.md": _fi("aa")}})
        out = list(iter_source_diffs(local, remote, skip_unchanged=False))
        assert len(out) == 1
        assert not out[0][3].has_changes

    def test_yields_intact_src_data_for_callers(self):
        """Callers (push) depend on src_data carrying base_path + files."""
        local = _fake_manifest({"claude": {"a.md": _fi("aa")}})
        out = list(iter_source_diffs(local, {}))
        _name, src_data, _remote_src, _diff = out[0]
        assert src_data["base_path"] == "/fake/claude"
        assert "a.md" in src_data["files"]


# ─── PullResult degradation fields ───────────────────────────────────────


class TestPullResultDegradationFields:
    def test_defaults_are_zero(self):
        r = PullResult()
        assert r.durability_fsync_failures == 0
        assert r.corrupt_peer_count == 0

    def test_fields_are_independent(self):
        r = PullResult(durability_fsync_failures=2, corrupt_peer_count=0)
        assert r.durability_fsync_failures == 2
        assert r.corrupt_peer_count == 0

    def test_clean_pull_has_zero_degradations(self, tmp_path, monkeypatch):
        """Happy-path autopull → PullResult carries zero degradation counts."""
        _setup_real_config(tmp_path, monkeypatch)
        iso = _redirect_sidecar(monkeypatch, tmp_path)

        r = runner.invoke(app, ["autopull"])
        assert r.exit_code == 0, (r.stdout, r.stderr)
        crumb = json.loads((iso / "last-autorun.json").read_text())
        # Clean pull should still be "success", not "degraded".
        assert crumb["outcome"] == "success"


# ─── Autopull breadcrumb: "degraded" outcome ─────────────────────────────


class TestAutopullDegradedBreadcrumb:
    """Full-coverage degraded breadcrumb: any of fsync / corrupt-peer /
    unknown-source / per-file failure triggers outcome='degraded' with
    a detail string enumerating which signal(s) fired.
    """

    def _prime_two_devices(self, tmp_path, monkeypatch):
        """Push from dev-a, switch to dev-b so there's something to pull."""
        storage_dir = tmp_path / "storage"
        claude_a = tmp_path / "machine_a" / ".claude"
        claude_b = tmp_path / "machine_b" / ".claude"
        _populate_claude(claude_a)
        claude_b.mkdir(parents=True)

        config_a, _ = _make_config(tmp_path / "a", storage_dir, claude_a, "dev-a", "Mac A")
        config_b, _ = _make_config(tmp_path / "b", storage_dir, claude_b, "dev-b", "Mac B")

        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        register_device(backend, "dev-a", "Mac A")
        register_device(backend, "dev-b", "Mac B")

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_a)
        _redirect_lock(monkeypatch, tmp_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
        assert runner.invoke(app, ["push"]).exit_code == 0

        # Switch to B for the upcoming autopull.
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_b)
        return backend, claude_a, claude_b

    def test_degraded_on_fsync_failure(self, tmp_path, monkeypatch):
        """REG-2: autopull breadcrumb outcome=degraded when fsync fails.

        test_track_1a.py:1017 already pins the stderr line. This pins
        the breadcrumb state, which mm status and ops monitoring read.
        """
        import mind_meld.fsutil as _fsu
        from mind_meld.errors import StorageError

        self._prime_two_devices(tmp_path, monkeypatch)
        iso = _redirect_sidecar(monkeypatch, tmp_path)

        real_fsync_dir = _fsu.fsync_dir

        def selective_boom(path):
            # Only fail on the pull's deferred-durability fsync (claude tree
            # or merged memory dir). Let init-path config backfill succeed.
            if "claude" in str(path) or "memory" in str(path):
                raise StorageError("simulated fsync failure")
            return real_fsync_dir(path)

        monkeypatch.setattr("mind_meld.fsutil.fsync_dir", selective_boom)

        r = runner.invoke(app, ["autopull"])
        assert r.exit_code == 0, (r.stdout, r.stderr)
        crumb = json.loads((iso / "last-autorun.json").read_text())
        assert crumb["outcome"] == "degraded"
        assert "fsync failed" in crumb["detail"]

    def test_degraded_on_corrupt_peer(self, tmp_path, monkeypatch):
        """Corrupt peer manifest → degraded with detail naming corrupt peer."""
        backend, _claude_a, _claude_b = self._prime_two_devices(tmp_path, monkeypatch)
        iso = _redirect_sidecar(monkeypatch, tmp_path)

        # Corrupt dev-a's manifest so the autopull from dev-b sees a corrupt peer.
        backend.put("manifests/dev-a/manifest.json.enc", b"not-a-valid-encrypted-blob")

        r = runner.invoke(app, ["autopull"])
        assert r.exit_code == 0, (r.stdout, r.stderr)
        crumb = json.loads((iso / "last-autorun.json").read_text())
        assert crumb["outcome"] == "degraded"
        assert "corrupt peer manifest" in crumb["detail"]

    def test_degraded_on_unknown_source(self, tmp_path, monkeypatch):
        """Deterministic stub: PullResult with unknown-source count → degraded.

        Uses a synthetic PullResult so the test doesn't depend on the corrupt-
        peer short-circuit ordering inside _pull_core.
        """
        import mind_meld.cli as cli_module
        from mind_meld.cli import PullResult

        _setup_real_config(tmp_path, monkeypatch)
        iso = _redirect_sidecar(monkeypatch, tmp_path)

        def fake_pull_core(*args, **kwargs):
            return PullResult(total_skipped_unknown_source=2, elapsed=0.1)

        monkeypatch.setattr(cli_module, "_pull_core", fake_pull_core)

        r = runner.invoke(app, ["autopull"])
        assert r.exit_code == 0, (r.stdout, r.stderr)
        crumb = json.loads((iso / "last-autorun.json").read_text())
        assert crumb["outcome"] == "degraded"
        assert "2 unknown source(s)" in crumb["detail"]

    def test_degraded_on_per_file_failure(self, tmp_path, monkeypatch):
        """Deterministic stub: PullResult with total_failed > 0 → degraded."""
        import mind_meld.cli as cli_module
        from mind_meld.cli import PullResult

        _setup_real_config(tmp_path, monkeypatch)
        iso = _redirect_sidecar(monkeypatch, tmp_path)

        def fake_pull_core(*args, **kwargs):
            return PullResult(total_failed=3, elapsed=0.1)

        monkeypatch.setattr(cli_module, "_pull_core", fake_pull_core)

        r = runner.invoke(app, ["autopull"])
        assert r.exit_code == 0, (r.stdout, r.stderr)
        crumb = json.loads((iso / "last-autorun.json").read_text())
        assert crumb["outcome"] == "degraded"
        assert "3 file(s) failed" in crumb["detail"]

    def test_degraded_detail_combines_all_four_signals(self, tmp_path, monkeypatch):
        """All 4 signals firing → detail enumerates each and joins with '; '.

        Uses a synthetic PullResult to pin the join behavior deterministically.
        The prior integration-style test depended on corrupt-peer short-
        circuit ordering and could only assert one signal in practice.
        """
        import mind_meld.cli as cli_module
        from mind_meld.cli import PullResult

        _setup_real_config(tmp_path, monkeypatch)
        iso = _redirect_sidecar(monkeypatch, tmp_path)

        def fake_pull_core(*args, **kwargs):
            return PullResult(
                durability_fsync_failures=1,
                corrupt_peer_count=2,
                total_skipped_unknown_source=3,
                total_failed=4,
                elapsed=0.1,
            )

        monkeypatch.setattr(cli_module, "_pull_core", fake_pull_core)

        r = runner.invoke(app, ["autopull"])
        assert r.exit_code == 0, (r.stdout, r.stderr)
        crumb = json.loads((iso / "last-autorun.json").read_text())
        assert crumb["outcome"] == "degraded"
        detail = crumb["detail"]
        # Every signal appears with its count.
        assert "fsync failed on 1 parent dir(s)" in detail
        assert "2 corrupt peer manifest(s)" in detail
        assert "3 unknown source(s)" in detail
        assert "4 file(s) failed" in detail
        # Signals joined with "; " — 4 signals → 3 joins.
        assert detail.count("; ") == 3


# ─── REG-1: GC safety for non-hex-sha blob paths ─────────────────────────


def test_gc_does_not_reap_non_hex_sha_blob(tmp_path, monkeypatch):
    """REG-1 (IRON RULE): post-Track-1C, `mm gc` routes non-hex-sha blob
    paths through the malformed-count path (skipped, never reaped). Without
    this, a corrupt peer manifest shipping a sentinel sha would cause the
    planted blob to get reaped as an "orphan" silently.
    """
    _setup_real_config(tmp_path, monkeypatch)
    _redirect_sidecar(monkeypatch, tmp_path)

    # Push so storage has a real manifest + referenced blobs.
    assert runner.invoke(app, ["push"]).exit_code == 0

    # Plant a blob with a non-hex-sha leaf. Pre-1C this was reaped by GC.
    storage_dir = tmp_path / "storage"
    backend = LocalBackend(storage_dir)
    planted_key = "data/dev-a/not-a-real-sha.enc"
    backend.put(planted_key, b"planted-sentinel-bytes")
    assert backend.exists(planted_key)

    # Run GC — should NOT reap the planted blob.
    r = runner.invoke(app, ["gc", "--verbose"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert backend.exists(planted_key), (
        "GC reaped a malformed (non-hex-sha) blob as an orphan — Track 1C safety invariant broken."
    )
    # Verbose output should surface it as malformed.
    assert "malformed" in r.stdout


# ─── 5E ship-fix (F3): autopull/autopush handle fleet-refusal cleanly ─────


class TestAutopullFleetRefusalBreadcrumb:
    """5E ship-fix (F3): a `typer.Exit(1)` raised by
    `_check_fleet_version_or_refuse` MUST be caught by autopull's
    `except typer.Exit` branch — NOT by the generic `except Exception`
    that logs the full refusal traceback to autopull.log on every
    Claude Code session start. The breadcrumb outcome must be
    `fleet-refused`, not `failed`."""

    def test_autopull_fleet_refusal_writes_clean_breadcrumb(self, tmp_path, monkeypatch):
        """Mixed-version fleet → autopull exits 0 (silent contract),
        writes outcome=fleet-refused, does NOT write to autopull.log."""
        from mind_meld.devices import register_device as _register
        from mind_meld.storage.keys import device_key

        # Standard two-device setup with one peer recorded as pre-v0.9.2.
        storage_dir = tmp_path / "storage"
        claude_dir = tmp_path / ".claude"
        _populate_claude(claude_dir)
        config_path, _ = _make_config(tmp_path, storage_dir, claude_dir, "dev-self", "Self")
        backend = LocalBackend(storage_dir)
        bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
        _register(backend, "dev-self", "Self")
        _register(backend, "dev-old", "OldPeer")
        # Manually plant a stale peer with pre-v0.9.2 version.
        old_data = {
            "device_id": "dev-old",
            "device_name": "OldPeer",
            "registered": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-04-01T00:00:00+00:00",
            "last_seen_version": "0.9.1",
        }
        backend.put(device_key("dev-old"), json.dumps(old_data).encode())

        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
        _redirect_lock(monkeypatch, tmp_path)
        iso = _redirect_sidecar(monkeypatch, tmp_path)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)

        r = runner.invoke(app, ["autopull"])
        # Silent contract: autopull NEVER non-zero exits on a "loud" refusal —
        # _error()'s typer.Exit(1) is caught and routed to the breadcrumb.
        assert r.exit_code == 0, (r.stdout, r.stderr)

        # Breadcrumb outcome must be `fleet-refused`, NOT `failed`.
        crumb = json.loads((iso / "last-autorun.json").read_text())
        assert crumb["outcome"] == "fleet-refused", (
            f"Expected fleet-refused; got {crumb['outcome']}. "
            f"This means typer.Exit was caught by `except Exception` "
            f"(F3 regression) and the user's autopull.log is now spammed "
            f"with refusal tracebacks on every hook fire."
        )

        # And NO autopull.log entry — _log_unexpected must not have run.
        log_path = iso / "autopull.log"
        assert not log_path.exists() or log_path.stat().st_size == 0, (
            "autopull.log should be empty for a fleet-refusal — "
            "_log_unexpected was called by mistake."
        )
