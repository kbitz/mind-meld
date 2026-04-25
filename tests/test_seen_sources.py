"""Tests for mind_meld.seen_sources — per-machine new-source tracker.

Pinned invariants:
  - Lazy init under flock seeds with caller's `initial` (the migration
    invariant for upgraders — without it every existing user gets
    spurious "new source!" hints for claude/gstack on first post-upgrade
    `mm status`).
  - Corrupt JSON / wrong shape → stderr warn + reset to `initial`.
  - compute_new_sources excludes seen, disabled, and explicit.
  - File mode 0600 (per-machine internal state, not user content).
"""

from __future__ import annotations

import json
import os
import stat
import threading

import pytest

from mind_meld import seen_sources


@pytest.fixture(autouse=True)
def _isolate_seen_dir(tmp_path, monkeypatch):
    """Redirect SEEN_DIR so tests never touch the user's real tracker."""
    iso = tmp_path / "mm_state"
    monkeypatch.setattr("mind_meld.seen_sources.SEEN_DIR", iso)


class TestRead:
    def test_lazy_init_on_missing_file_seeds_with_initial(self):
        """Migration invariant: first post-upgrade read seeds the file
        with currently-resolved sources. Existing users don't see
        spurious 'new source!' hints for claude/gstack."""
        result = seen_sources.read(initial=["claude", "gstack"])
        assert result == {"claude", "gstack"}
        assert seen_sources.seen_path().exists()
        on_disk = json.loads(seen_sources.seen_path().read_text())
        assert sorted(on_disk) == ["claude", "gstack"]

    def test_seeded_file_returned_unchanged_on_subsequent_read(self):
        seen_sources.read(initial=["claude", "gstack"])
        # Second read with different `initial` — file already seeded,
        # initial is ignored.
        result = seen_sources.read(initial=["completely", "different"])
        assert result == {"claude", "gstack"}

    def test_empty_file_treated_as_missing(self):
        """A zero-byte file (truncated mid-write, or test artifact) should
        be re-seeded under flock, not parsed as invalid JSON."""
        seen_sources.SEEN_DIR.mkdir(parents=True, exist_ok=True)
        seen_sources.seen_path().write_text("")
        result = seen_sources.read(initial=["claude"])
        assert result == {"claude"}
        # File now contains the seed, not empty.
        assert seen_sources.seen_path().read_text().strip() != ""

    def test_corrupt_json_warns_and_resets(self, capsys):
        seen_sources.SEEN_DIR.mkdir(parents=True, exist_ok=True)
        seen_sources.seen_path().write_text("{not valid json")
        result = seen_sources.read(initial=["claude", "gstack"])
        assert result == {"claude", "gstack"}
        captured = capsys.readouterr()
        assert "seen-sources.json corrupt" in captured.err
        # File reset to seed.
        on_disk = json.loads(seen_sources.seen_path().read_text())
        assert sorted(on_disk) == ["claude", "gstack"]

    def test_wrong_shape_warns_and_resets(self, capsys):
        """A JSON dict where a list was expected, or a list of non-strings."""
        seen_sources.SEEN_DIR.mkdir(parents=True, exist_ok=True)
        seen_sources.seen_path().write_text('{"not": "a list"}')
        result = seen_sources.read(initial=["claude"])
        assert result == {"claude"}
        captured = capsys.readouterr()
        assert "malformed" in captured.err

    def test_wrong_shape_list_of_non_strings(self, capsys):
        seen_sources.SEEN_DIR.mkdir(parents=True, exist_ok=True)
        seen_sources.seen_path().write_text("[1, 2, 3]")
        result = seen_sources.read(initial=["claude"])
        assert result == {"claude"}
        captured = capsys.readouterr()
        assert "malformed" in captured.err

    def test_file_mode_0600(self):
        seen_sources.read(initial=["claude"])
        mode = stat.S_IMODE(os.stat(seen_sources.seen_path()).st_mode)
        assert mode == 0o600


class TestWrite:
    def test_write_replaces_contents(self):
        seen_sources.write({"claude", "gstack"})
        on_disk = json.loads(seen_sources.seen_path().read_text())
        assert sorted(on_disk) == ["claude", "gstack"]

        seen_sources.write({"claude", "gstack", "codex"})
        on_disk = json.loads(seen_sources.seen_path().read_text())
        assert sorted(on_disk) == ["claude", "codex", "gstack"]

    def test_write_sorts_for_determinism(self):
        seen_sources.write({"zebra", "alpha", "mike"})
        # Round-trip via raw bytes: confirm sorted, not just the set.
        raw = seen_sources.seen_path().read_text().strip()
        assert raw == json.dumps(["alpha", "mike", "zebra"])

    def test_write_mode_0600(self):
        seen_sources.write({"claude"})
        mode = stat.S_IMODE(os.stat(seen_sources.seen_path()).st_mode)
        assert mode == 0o600


class TestConcurrency:
    def test_concurrent_callers_serialize_under_flock(self):
        """Two threads calling read() simultaneously on a missing file
        either serialize or both seed idempotently. Final state must be
        consistent and contain the seed."""
        results: list[set[str]] = []
        errors: list[BaseException] = []

        def call_read():
            try:
                results.append(seen_sources.read(initial=["claude", "gstack"]))
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=call_read) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # All readers see the same set.
        assert all(r == {"claude", "gstack"} for r in results)
        # File on disk is consistent with that set.
        on_disk = json.loads(seen_sources.seen_path().read_text())
        assert sorted(on_disk) == ["claude", "gstack"]


class TestAcknowledge:
    """Atomic read-modify-write for concurrent enable/disable callers.
    Codex 2026-04-25: previous read+update+write split was race-prone;
    acknowledge() holds flock across the whole RMW."""

    def test_seeds_with_initial_when_file_missing(self):
        result = seen_sources.acknowledge(["codex"], initial=["claude", "gstack"])
        assert result == {"claude", "gstack", "codex"}

    def test_unions_names_with_existing_set(self):
        seen_sources.write({"claude", "gstack"})
        result = seen_sources.acknowledge(["codex"], initial=[])
        assert result == {"claude", "gstack", "codex"}

    def test_concurrent_acknowledge_no_lost_updates(self):
        """Two concurrent acknowledge calls each adding distinct names —
        final state must contain both. The pre-fix split read+write would
        lose one of them; acknowledge serializes under flock."""
        seen_sources.write({"claude", "gstack"})
        results: list[set[str]] = []
        errors: list[BaseException] = []

        def call_ack(name: str):
            try:
                results.append(seen_sources.acknowledge([name], initial=[]))
            except BaseException as e:
                errors.append(e)

        threads = [
            threading.Thread(target=call_ack, args=(n,))
            for n in ["codex", "cursor", "aider", "shell"]
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # File on disk must contain ALL added names.
        on_disk = set(json.loads(seen_sources.seen_path().read_text()))
        assert {"claude", "gstack", "codex", "cursor", "aider", "shell"} <= on_disk

    def test_idempotent_redundant_names(self):
        result1 = seen_sources.acknowledge(["claude"], initial=["claude", "gstack"])
        result2 = seen_sources.acknowledge(["claude"], initial=[])
        assert result1 == result2 == {"claude", "gstack"}


class TestStorageErrorHandling:
    """Codex 2026-04-25: write() and acknowledge() must catch StorageError
    (which fsutil.atomic_write_bytes raises) AND OSError, otherwise a
    disk-full / permission flip can crash the calling command through a
    non-load-bearing tracker."""

    def test_write_swallows_storage_error_with_warning(self, monkeypatch, capsys):
        from mind_meld.errors import StorageError

        def boom(*_a, **_kw):
            raise StorageError("simulated disk full")

        monkeypatch.setattr("mind_meld.seen_sources.fsutil.atomic_write_bytes", boom)
        # No exception bubbles up.
        seen_sources.write({"claude"})
        captured = capsys.readouterr()
        assert "failed to write seen-sources.json" in captured.err

    def test_acknowledge_swallows_oserror_with_warning(self, monkeypatch, capsys):
        # Seed the file so acknowledge takes the read+merge path, not the
        # missing-file branch.
        seen_sources.write({"claude"})

        # acknowledge uses os.write in-place (not fsutil.atomic_write_bytes)
        # to preserve the inode under the flock — see the inode-swap
        # rationale in the module. Mock os.write to simulate a disk failure.
        real_write = os.write

        def boom(fd, data):
            # Only fail writes targeting an actual file fd (not stderr/stdout
            # used by capsys).
            if fd > 2:
                raise OSError("simulated disk full")
            return real_write(fd, data)

        monkeypatch.setattr("mind_meld.seen_sources.os.write", boom)
        result = seen_sources.acknowledge(["codex"], initial=[])
        assert result == {"claude", "codex"}
        captured = capsys.readouterr()
        assert "failed to write seen-sources.json" in captured.err


class TestComputeNewSources:
    def test_returns_defaults_minus_seen(self):
        new = seen_sources.compute_new_sources(
            seen={"claude"},
            default_names=["claude", "gstack", "codex"],
            disabled=[],
            explicit_names=[],
        )
        assert new == ["gstack", "codex"]

    def test_excludes_disabled(self):
        new = seen_sources.compute_new_sources(
            seen=set(),
            default_names=["claude", "gstack", "codex"],
            disabled=["gstack"],
            explicit_names=[],
        )
        assert new == ["claude", "codex"]

    def test_excludes_explicit_user_sources(self):
        """If user has gstack in [[sync.sources]] but the seen tracker
        is stale, no hint should be shown — they're already syncing."""
        new = seen_sources.compute_new_sources(
            seen=set(),
            default_names=["claude", "gstack", "codex"],
            disabled=[],
            explicit_names=["gstack"],
        )
        assert new == ["claude", "codex"]

    def test_preserves_default_names_order(self):
        """Surfacing order matches DEFAULT_SOURCES so claude appears
        before gstack before codex if all three were new."""
        new = seen_sources.compute_new_sources(
            seen=set(),
            default_names=["claude", "gstack", "codex"],
            disabled=[],
            explicit_names=[],
        )
        assert new == ["claude", "gstack", "codex"]

    def test_no_new_sources_when_all_acknowledged(self):
        new = seen_sources.compute_new_sources(
            seen={"claude", "gstack", "codex"},
            default_names=["claude", "gstack", "codex"],
            disabled=[],
            explicit_names=[],
        )
        assert new == []
