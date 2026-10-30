"""Tests for mind_meld.storage.local — local folder backend."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mind_meld import fsutil
from mind_meld.errors import StorageError, StorageNotFound
from mind_meld.storage import local as storage_local
from mind_meld.storage.local import LocalBackend
from tests.syncthing_folder import SyncthingFolder


def _always_valid(_: Path) -> bool:
    return True


def _always_invalid(_: Path) -> bool:
    return False


class TestLocalBackend:
    @pytest.fixture
    def backend(self, tmp_path):
        return LocalBackend(tmp_path / "storage")

    def test_put_get_round_trip(self, backend):
        backend.put("data/abc/hash.enc", b"hello")
        assert backend.get("data/abc/hash.enc") == b"hello"

    def test_put_creates_parents(self, backend):
        backend.put("deep/nested/dir/file.enc", b"data")
        assert backend.get("deep/nested/dir/file.enc") == b"data"

    def test_get_missing_raises(self, backend):
        with pytest.raises(StorageError, match="not found"):
            backend.get("nonexistent.enc")

    def test_exists(self, backend):
        assert not backend.exists("test.enc")
        backend.put("data/x/test.enc", b"data")
        assert backend.exists("data/x/test.enc")

    def test_delete(self, backend):
        backend.put("data/x/test.enc", b"data")
        assert backend.exists("data/x/test.enc")
        backend.delete("data/x/test.enc")
        assert not backend.exists("data/x/test.enc")

    def test_delete_nonexistent_is_noop(self, backend):
        backend.delete("nonexistent.enc")  # should not raise

    def test_list_keys(self, backend):
        backend.put("data/abc/hash1.enc", b"1")
        backend.put("data/abc/hash2.enc", b"2")
        backend.put("data/def/hash3.enc", b"3")
        backend.put("manifests/abc/manifest.json.enc", b"m")

        keys = backend.list_keys("data/")
        assert len(keys) == 3
        assert "data/abc/hash1.enc" in keys
        assert "data/def/hash3.enc" in keys

    def test_list_keys_empty_prefix(self, backend):
        assert backend.list_keys("nonexistent/") == []

    def test_list_keys_specific_device(self, backend):
        backend.put("data/abc/h1.enc", b"1")
        backend.put("data/def/h2.enc", b"2")
        keys = backend.list_keys("data/abc/")
        assert len(keys) == 1
        assert "data/abc/h1.enc" in keys

    def test_atomic_write(self, backend):
        backend.put("data/x/test.enc", b"original")
        assert backend.get("data/x/test.enc") == b"original"
        backend.put("data/x/test.enc", b"updated")
        assert backend.get("data/x/test.enc") == b"updated"

    def test_binary_data(self, backend):
        data = bytes(range(256))
        backend.put("data/x/binary.enc", data)
        assert backend.get("data/x/binary.enc") == data

    def test_large_data(self, backend):
        data = b"x" * 1_000_000
        backend.put("data/x/large.enc", data)
        assert backend.get("data/x/large.enc") == data


class TestFsyncRouting:
    """Verify put() passes the right fsync flag to fsutil per key prefix."""

    @pytest.fixture
    def backend(self, tmp_path):
        return LocalBackend(tmp_path / "storage")

    @pytest.fixture
    def spy(self, monkeypatch):
        calls: list[dict] = []
        real_write = fsutil.atomic_write_bytes

        def spy_write(path, data, *, fsync=False, mode=None):
            calls.append(
                {
                    "path": path,
                    "fsync": fsync,
                    "mode": mode,
                    "len": len(data),
                }
            )
            real_write(path, data, fsync=fsync, mode=mode)

        # Patch the symbol used inside storage_local, which binds
        # fsutil.atomic_write_bytes at import time.
        monkeypatch.setattr(storage_local.fsutil, "atomic_write_bytes", spy_write)
        return calls

    def test_manifests_prefix_uses_fsync_true(self, backend, spy):
        backend.put("manifests/abc/manifest.json.enc", b"m")
        assert len(spy) == 1
        assert spy[0]["fsync"] is True

    def test_devices_prefix_uses_fsync_true(self, backend, spy):
        backend.put("devices/abc.json", b"{}")
        assert len(spy) == 1
        assert spy[0]["fsync"] is True

    def test_data_prefix_uses_fsync_false(self, backend, spy):
        backend.put("data/abc/hash.enc", b"blob")
        assert len(spy) == 1
        assert spy[0]["fsync"] is False

    def test_unknown_prefix_defaults_to_fsync_false(self, backend, spy):
        """Unknown prefixes get the safe default (no fsync)."""
        backend.put("other/x.bin", b"x")
        assert len(spy) == 1
        assert spy[0]["fsync"] is False

    @pytest.mark.parametrize(
        "key",
        [
            "mm-crypto-init",
            "mm-crypto-init.preserved-abcd1234-20260101T000000Z",
        ],
    )
    def test_crypto_init_keys_use_fsync_true(self, backend, spy, key):
        backend.put(key, b"blob")
        assert spy[-1]["fsync"] is True
        assert spy[-1]["mode"] == 0o600

    def test_all_storage_writes_use_mode_0600(self, backend, spy):
        """All storage keys hold encrypted secrets — explicit 0600 to
        prevent world-readable leaks via umask on new files."""
        backend.put("manifests/dev1/manifest.json.enc", b"a")
        backend.put("devices/dev1.json", b"b")
        backend.put("data/dev1/abc.enc", b"c")
        assert len(spy) == 3
        for c in spy:
            assert c["mode"] == 0o600, f"storage write to {c['path']} used mode {c['mode']!r}"


class TestConflictDetection:
    """find_conflict_copies scope guard + predicate behavior."""

    @pytest.fixture
    def backend(self, tmp_path):
        return LocalBackend(tmp_path / "storage")

    def test_find_no_conflicts(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"data")
        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            _always_valid,
        )
        assert conflicts == []

    def test_find_icloud_conflicts(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        conflict_path = backend.root / "manifests" / "abc" / "manifest.json 2.enc"
        conflict_path.write_bytes(b"conflict")

        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            _always_valid,
        )
        assert len(conflicts) == 1

    def test_find_multiple_icloud_conflicts(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        for i in range(2, 5):
            path = backend.root / "manifests" / "abc" / f"manifest.json {i}.enc"
            path.write_bytes(f"conflict {i}".encode())

        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            _always_valid,
        )
        assert len(conflicts) == 3

    def test_find_dropbox_conflicts(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        conflict_name = "manifest.json (conflicted copy 2026-03-18).enc"
        conflict_path = backend.root / "manifests" / "abc" / conflict_name
        conflict_path.write_bytes(b"conflict")

        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            _always_valid,
        )
        assert len(conflicts) == 1

    def test_delete_conflicts(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        icloud = backend.root / "manifests" / "abc" / "manifest.json 2.enc"
        icloud.write_bytes(b"icloud conflict")
        dropbox = (
            backend.root / "manifests" / "abc" / "manifest.json (conflicted copy 2026-03-18).enc"
        )
        dropbox.write_bytes(b"dropbox conflict")

        count = backend.delete_conflict_copies(
            "manifests/abc/manifest.json.enc",
            _always_valid,
        )
        assert count == 2
        assert not icloud.exists()
        assert not dropbox.exists()


class TestPutExclusive:
    """Atomic create-only primitive for bootstrap blobs like mm-crypto-init."""

    @pytest.fixture
    def backend(self, tmp_path):
        return LocalBackend(tmp_path / "storage")

    def test_put_exclusive_creates_file(self, backend):
        backend.put_exclusive("mm-crypto-init", b"hello")
        assert backend.get("mm-crypto-init") == b"hello"

    def test_put_exclusive_creates_parents(self, backend):
        backend.put_exclusive("deep/nested/init.blob", b"data")
        assert backend.get("deep/nested/init.blob") == b"data"

    def test_put_exclusive_fails_if_target_exists(self, backend):
        backend.put_exclusive("mm-crypto-init", b"first")
        with pytest.raises(StorageError, match="already exists"):
            backend.put_exclusive("mm-crypto-init", b"second")
        assert backend.get("mm-crypto-init") == b"first"

    def test_put_exclusive_leaves_no_temp_on_success(self, backend, tmp_path):
        backend.put_exclusive("mm-crypto-init", b"data")
        parent = tmp_path / "storage"
        entries = sorted(p.name for p in parent.iterdir())
        assert entries == ["mm-crypto-init"]

    def test_put_exclusive_leaves_no_temp_on_failure(self, backend, tmp_path):
        backend.put_exclusive("mm-crypto-init", b"first")
        with pytest.raises(StorageError):
            backend.put_exclusive("mm-crypto-init", b"second")
        parent = tmp_path / "storage"
        entries = sorted(p.name for p in parent.iterdir())
        assert entries == ["mm-crypto-init"]

    @pytest.mark.parametrize("failing", ["write", "fsync"])
    def test_put_exclusive_removes_its_temp_when_write_or_fsync_fails(
        self, backend, tmp_path, monkeypatch, failing
    ):
        import os

        parent = tmp_path / "storage"

        def fail(*args, **kwargs):
            raise OSError(28, "No space left on device")

        # Scoped to the failing call: monkeypatch.undo() would also strip conftest's
        # autouse isolation fixtures for the rest of the test.
        with monkeypatch.context() as patch:
            patch.setattr(os, failing if failing == "fsync" else "write", fail)
            with pytest.raises(OSError):
                backend.put_exclusive("mm-crypto-init", b"data")
        assert sorted(p.name for p in parent.iterdir()) == []
        # The failure left nothing behind, so a retry succeeds.
        backend.put_exclusive("mm-crypto-init", b"data")
        assert sorted(p.name for p in parent.iterdir()) == ["mm-crypto-init"]
        assert backend.get("mm-crypto-init") == b"data"

    def test_put_exclusive_failed_cleanup_leaves_the_temp_for_the_next_refusal(
        self, backend, tmp_path, monkeypatch
    ):
        import os

        def fail_write(*args, **kwargs):
            raise OSError(28, "No space left on device")

        def fail_unlink(*args, **kwargs):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(os, "write", fail_write)
        monkeypatch.setattr(os, "unlink", fail_unlink)
        with pytest.raises(OSError, match="No space"):
            backend.put_exclusive("mm-crypto-init", b"data")
        leftovers = [p.name for p in (tmp_path / "storage").iterdir()]
        assert len(leftovers) == 1 and leftovers[0].endswith(".tmp")


class TestExtensionlessConflictRegex:
    """iCloud conflict detection for files without an extension (e.g. mm-crypto-init).

    No predicate passed — crypto-v2 bootstrap validates candidates itself
    after find returns."""

    @pytest.fixture
    def backend(self, tmp_path):
        return LocalBackend(tmp_path / "storage")

    def test_find_extensionless_icloud_conflict(self, backend):
        backend.put("mm-crypto-init", b"canonical")
        (backend.root / "mm-crypto-init 2").write_bytes(b"conflict")
        conflicts = backend.find_conflict_copies("mm-crypto-init")
        assert len(conflicts) == 1
        assert conflicts[0].name == "mm-crypto-init 2"

    def test_find_multiple_extensionless_icloud_conflicts(self, backend):
        backend.put("mm-crypto-init", b"canonical")
        for i in (2, 3, 5):
            (backend.root / f"mm-crypto-init {i}").write_bytes(b"conflict")
        conflicts = backend.find_conflict_copies("mm-crypto-init")
        assert len(conflicts) == 3

    def test_find_extensionless_dropbox_conflict(self, backend):
        backend.put("mm-crypto-init", b"canonical")
        (backend.root / "mm-crypto-init (conflicted copy 2026-04-22)").write_bytes(b"x")
        conflicts = backend.find_conflict_copies("mm-crypto-init")
        assert len(conflicts) == 1

    def test_delete_extensionless_conflicts(self, backend):
        backend.put("mm-crypto-init", b"canonical")
        (backend.root / "mm-crypto-init 2").write_bytes(b"a")
        (backend.root / "mm-crypto-init 3").write_bytes(b"b")
        count = backend.delete_conflict_copies("mm-crypto-init")
        assert count == 2
        assert backend.exists("mm-crypto-init")

    def test_extensionless_does_not_match_extension_file(self, backend):
        """A file named 'mm-crypto-init 2.txt' is NOT a conflict copy of 'mm-crypto-init'."""
        backend.put("mm-crypto-init", b"canonical")
        (backend.root / "mm-crypto-init 2.txt").write_bytes(b"unrelated")
        conflicts = backend.find_conflict_copies("mm-crypto-init")
        assert conflicts == []

    def test_extension_file_still_requires_matching_ext(self, backend):
        """Regression: 'manifest.json 2.enc' still matches, 'manifest.json 2' doesn't."""
        backend.put("manifests/abc/manifest.json.enc", b"x")
        (backend.root / "manifests" / "abc" / "manifest.json 2.enc").write_bytes(b"y")
        (backend.root / "manifests" / "abc" / "manifest.json 2").write_bytes(b"z")
        conflicts = backend.find_conflict_copies("manifests/abc/manifest.json.enc")
        assert len(conflicts) == 1
        assert conflicts[0].name == "manifest.json 2.enc"


class TestPredicateBehavior:
    """Predicate controls which regex-matching candidates become confirmed conflicts.

    The predicate is OPTIONAL (None default accepts all candidates) so crypto-v2
    bootstrap can use the 1-arg form. Manifest-recovery callers pass a validator
    that decrypts + deserialize_manifest-shape-checks each candidate."""

    @pytest.fixture
    def backend(self, tmp_path):
        return LocalBackend(tmp_path / "storage")

    def test_no_predicate_accepts_all_candidates(self, backend):
        """Omitting the predicate returns every regex-matching sibling."""
        backend.put("manifests/abc/manifest.json.enc", b"original")
        bogus = backend.root / "manifests" / "abc" / "manifest.json 2.enc"
        bogus.write_bytes(b"random bytes")

        conflicts = backend.find_conflict_copies("manifests/abc/manifest.json.enc")
        assert len(conflicts) == 1

    def test_predicate_false_rejects_all(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        bogus = backend.root / "manifests" / "abc" / "manifest.json 2.enc"
        bogus.write_bytes(b"not a real conflict")

        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            _always_invalid,
        )
        assert conflicts == []
        assert bogus.exists()

    def test_predicate_exception_treated_as_false(self, backend, capsys):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        bogus = backend.root / "manifests" / "abc" / "manifest.json 2.enc"
        bogus.write_bytes(b"suspect")

        def exploding(_: Path) -> bool:
            raise RuntimeError("validator bug")

        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            exploding,
        )
        assert conflicts == []
        captured = capsys.readouterr()
        assert "validator raised" in captured.err
        assert bogus.exists()

    def test_delete_only_removes_predicate_approved(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        legit = backend.root / "manifests" / "abc" / "manifest.json 2.enc"
        legit.write_bytes(b"legit conflict")
        bogus = backend.root / "manifests" / "abc" / "manifest.json 3.enc"
        bogus.write_bytes(b"bogus file")

        def selective(p: Path) -> bool:
            return p.read_bytes() == b"legit conflict"

        count = backend.delete_conflict_copies(
            "manifests/abc/manifest.json.enc",
            selective,
        )
        assert count == 1
        assert not legit.exists()
        assert bogus.exists()

    def test_adversarial_backup_name_not_matched(self, backend):
        """A file like 'manifest.json.enc.backup' does not match the pattern."""
        backend.put("manifests/abc/manifest.json.enc", b"original")
        backup = backend.root / "manifests" / "abc" / "manifest.json.enc.backup"
        backup.write_bytes(b"user backup")

        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            _always_valid,
        )
        assert conflicts == []

    def test_adversarial_wrong_extension_not_matched(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        wrong_ext = backend.root / "manifests" / "abc" / "manifest.json 2.txt"
        wrong_ext.write_bytes(b"not encrypted")

        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            _always_valid,
        )
        assert conflicts == []

    def test_adversarial_non_numeric_middle_not_matched(self, backend):
        """'manifest.json foo.enc' does not match (middle must be a number)."""
        backend.put("manifests/abc/manifest.json.enc", b"original")
        weird = backend.root / "manifests" / "abc" / "manifest.json foo.enc"
        weird.write_bytes(b"weird")

        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            _always_valid,
        )
        assert conflicts == []


_OSC_BEL = "\x1b]52;c;ZXZpbA==\x07"
# Nested CSI that one strip pass turns into OSC 52 (BEL-terminated).
_NESTED_OSC_BEL = "\x1b\x1b[31m]52;c;ZXZpbA==\x07"
_DROPBOX_OSC = f"manifest.json (conflicted copy 2026-03-18{_OSC_BEL}).enc"
_DROPBOX_NESTED = f"manifest.json (conflicted copy 2026-03-18{_NESTED_OSC_BEL}).enc"
_DROPBOX_CONTROLS = "manifest.json (conflicted copy 2026-03-18\x07\x08\x7f).enc"
_ICLOUD_LF = "manifest.json\n2.enc"


def _assert_single_plain_warning(err: str, *sentinels: str) -> None:
    assert err, "expected a rejection warning on stderr"
    assert err.startswith("mm: warning:"), err
    assert err.endswith("\n")
    assert err.count("\n") == 1, repr(err)
    body = err[:-1]
    assert all(ch.isprintable() for ch in body), repr(body)
    assert "File left in place" in err
    assert "storage folder" in err
    assert "mm gc --conflicts" not in err
    for sentinel in sentinels:
        assert sentinel in err


class TestRejectedFilenameWarnings:
    """Rejected storage siblings warn with safe fields and keep original files."""

    @pytest.fixture
    def backend(self, tmp_path):
        return LocalBackend(tmp_path / "storage")

    def _matching_sibling(self, backend: LocalBackend, name: str, payload: bytes) -> Path:
        backend.put("manifests/abc/manifest.json.enc", b"original")
        path = backend.root / "manifests" / "abc" / name
        path.write_bytes(payload)
        return path

    @pytest.mark.parametrize(
        "name",
        [_DROPBOX_OSC, _DROPBOX_NESTED, _DROPBOX_CONTROLS, _ICLOUD_LF],
    )
    def test_false_validator_warns_safely_and_preserves_file(self, backend, capsys, name):
        payload = b"rejected-bytes"
        sibling = self._matching_sibling(backend, name, payload)
        seen: list[Path] = []

        def reject(path: Path) -> bool:
            seen.append(path)
            return False

        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            reject,
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        _assert_single_plain_warning(captured.err, "failed validation", "manifest.json")
        assert "\x1b" not in captured.err
        assert "\x07" not in captured.err
        assert conflicts == []
        assert seen == [sibling]
        assert sibling.exists()
        assert sibling.read_bytes() == payload
        assert sibling.name == name

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError(f"validator {_OSC_BEL} boom"),
            RuntimeError(_NESTED_OSC_BEL),
            RuntimeError("bad\nreason\r\t"),
            RuntimeError(""),
            RuntimeError("\x07\x08\x1b"),
            RuntimeError("\ud800"),
        ],
        ids=[
            "osc",
            "nested",
            "record-controls",
            "empty",
            "all-control",
            "surrogate",
        ],
    )
    def test_raised_validator_sanitizes_path_and_reason(self, backend, capsys, exc):
        payload = b"suspect"
        sibling = self._matching_sibling(backend, _DROPBOX_NESTED, payload)
        seen: list[Path] = []

        def exploding(path: Path) -> bool:
            seen.append(path)
            raise exc

        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            exploding,
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        _assert_single_plain_warning(
            captured.err,
            "validator raised",
            "RuntimeError",
            "manifest.json",
        )
        assert "\x1b" not in captured.err
        assert "\x07" not in captured.err
        assert "\n" not in captured.err[:-1]
        assert conflicts == []
        assert seen == [sibling]
        assert sibling.read_bytes() == payload

    def test_mixed_candidates_continue_and_keep_original_paths(self, backend, capsys):
        backend.put("manifests/abc/manifest.json.enc", b"original")
        parent = backend.root / "manifests" / "abc"
        legit = parent / "manifest.json 2.enc"
        rejected = parent / "manifest.json 3.enc"
        raising = parent / "manifest.json (conflicted copy 2026-03-18).enc"
        legit.write_bytes(b"legit conflict")
        rejected.write_bytes(b"bogus file")
        raising.write_bytes(b"raises")
        seen: list[Path] = []

        def selective(path: Path) -> bool:
            seen.append(path)
            if path.name == raising.name:
                raise RuntimeError("validator bug")
            return path.read_bytes() == b"legit conflict"

        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            selective,
        )
        captured = capsys.readouterr()
        assert len(seen) == 3
        assert {p.name for p in seen} == {legit.name, rejected.name, raising.name}
        assert [p.name for p in conflicts] == ["manifest.json 2.enc"]
        assert any(p is conflicts[0] for p in seen)
        assert conflicts[0] == legit
        assert legit.read_bytes() == b"legit conflict"
        assert rejected.read_bytes() == b"bogus file"
        assert raising.read_bytes() == b"raises"
        assert "failed validation" in captured.err
        assert "validator raised" in captured.err
        assert "RuntimeError" in captured.err
        assert "\x1b" not in captured.err

    def test_full_path_including_parent_is_sanitized(self, tmp_path, capsys):
        root = tmp_path / f"store{_OSC_BEL}dir" / "storage"
        backend = LocalBackend(root)
        sibling = self._matching_sibling(backend, _DROPBOX_OSC, b"x")
        conflicts = backend.find_conflict_copies(
            "manifests/abc/manifest.json.enc",
            _always_invalid,
        )
        captured = capsys.readouterr()
        _assert_single_plain_warning(captured.err, "failed validation")
        assert "\x1b" not in captured.err
        assert "\x07" not in captured.err
        assert "store" in captured.err
        assert "dir" in captured.err
        assert conflicts == []
        assert sibling.exists()


STAMP = "sync-conflict-20260101-101010-ABCDEFG"


class TestSyncthingNames:
    """Track 87A: foreign names are ignored and Syncthing conflict names are known."""

    @pytest.fixture
    def backend(self, tmp_path):
        return LocalBackend(tmp_path / "storage")

    # Coverage audit value card:
    # Value: protects=storage listing keeps surviving keys when a directory disappears;
    # fails_when=a vanished subtree aborts enumeration or hides a stable sibling;
    # why_new=existing removal scenarios remove files after listing, not a subtree during it;
    # seam=none; no_claim=live Syncthing timing (temporary directory removed at scandir).
    def test_directory_vanishing_during_walk_preserves_other_keys(self, backend, monkeypatch):
        backend.put("devices/stable.json", b"{}")
        gone = backend.root / "departing"
        gone.mkdir()
        original_scandir = os.scandir

        def scandir(path):
            if Path(path) == gone:
                gone.rmdir()
            return original_scandir(path)

        monkeypatch.setattr(os, "scandir", scandir)

        assert backend.list_keys("") == ["devices/stable.json"]
        assert not gone.exists()

    def test_list_keys_skips_dot_prefixed_and_syncthing_temp_names(self, backend):
        backend.put("manifests/abc/manifest.json.enc", b"m")
        backend.put("data/abc/" + "a" * 64 + ".enc", b"b")
        backend.put("devices/abc.json", b"{}")
        backend.put("manifests/abc/.syncthing.manifest.json.enc.tmp", b"x")
        backend.put("data/abc/~syncthing~blob.tmp", b"x")
        backend.put("devices/.stfolder-note", b"x")
        backend.put(".stversions/manifests/abc/manifest.json~20260101-101010.enc", b"old")
        backend.put(".DS_Store", b"x")

        assert backend.list_keys("") == [
            "data/abc/" + "a" * 64 + ".enc",
            "devices/abc.json",
            "manifests/abc/manifest.json.enc",
        ]
        assert backend.list_keys("manifests/") == ["manifests/abc/manifest.json.enc"]

    def test_list_foreign_keys_reports_what_list_keys_skips(self, backend):
        backend.put("devices/abc.json", b"{}")
        backend.put("manifests/abc/.syncthing.manifest.json.enc.tmp", b"x")
        backend.put(".stversions/devices/abc.json~20260101-101010", b"old")
        backend.put(".stversions/deeper/still/old.enc", b"old")

        assert backend.list_foreign_keys() == [
            ".stversions/deeper/still/old.enc",
            ".stversions/devices/abc.json~20260101-101010",
            "manifests/abc/.syncthing.manifest.json.enc.tmp",
        ]
        assert backend.list_foreign_keys("manifests/") == [
            "manifests/abc/.syncthing.manifest.json.enc.tmp"
        ]

    @pytest.mark.parametrize(
        ("key", "name"),
        [
            ("manifests/abc/manifest.json.enc", f"manifest.json.{STAMP}.enc"),
            ("manifests/abc/manifest.json.enc", "manifest.json.sync-conflict-20260101-101010.enc"),
            (
                "manifests/abc/manifest.json.enc",
                f"manifest.json.{STAMP}.sync-conflict-20260102-111111-ZYXWVUT.enc",
            ),
            ("mm-crypto-init", f"mm-crypto-init.{STAMP}"),
            ("devices/abc.json", f"abc.{STAMP}.json"),
        ],
    )
    def test_find_conflict_copies_recognizes_syncthing_names(self, backend, key, name):
        backend.put(key, b"canonical")
        sibling = (backend.root / key).parent / name
        sibling.write_bytes(b"copy")

        assert backend.find_conflict_copies(key) == [sibling]

    def test_find_conflict_copies_ignores_other_keys_and_foreign_names(self, backend):
        key = "manifests/abc/manifest.json.enc"
        backend.put(key, b"canonical")
        folder = backend.root / "manifests/abc"
        (folder / f"other.json.{STAMP}.enc").write_bytes(b"other key")
        (folder / f".syncthing.manifest.json.{STAMP}.enc.tmp").write_bytes(b"temp")
        (folder / f"~syncthing~manifest.json.{STAMP}.enc").write_bytes(b"temp")
        (folder / "manifest.json.preserved-ab12cd34-20260101.enc").write_bytes(b"preserved")

        assert backend.find_conflict_copies(key) == []

    def test_crypto_init_preserved_copies_never_match(self, backend):
        backend.put("mm-crypto-init", b"canonical")
        (backend.root / "mm-crypto-init.preserved-ab12cd34-20260101T101010Z").write_bytes(b"kept")

        assert backend.find_conflict_copies("mm-crypto-init") == []

    def test_candidate_gone_at_validation_is_skipped_silently(self, backend, capsys):
        key = "manifests/abc/manifest.json.enc"
        backend.put(key, b"canonical")
        copy = backend.root / f"manifests/abc/manifest.json.{STAMP}.enc"
        copy.write_bytes(b"copy")

        def replaced_while_validating(path: Path) -> bool:
            path.unlink()
            return False

        assert backend.find_conflict_copies(key, replaced_while_validating) == []
        assert capsys.readouterr().err == ""

    def test_candidate_that_fails_validation_still_warns(self, backend, capsys):
        key = "manifests/abc/manifest.json.enc"
        backend.put(key, b"canonical")
        (backend.root / f"manifests/abc/manifest.json.{STAMP}.enc").write_bytes(b"copy")

        assert backend.find_conflict_copies(key, _always_invalid) == []
        assert "ignoring suspicious file" in capsys.readouterr().err


class TestVanishedReads:
    """Track 87A: a file the caller has seen vanish is retried, and absence is not corruption."""

    @pytest.fixture
    def folder(self, tmp_path):
        return SyncthingFolder(tmp_path / "storage")

    def test_vanished_file_that_returns_is_read(self, folder):
        folder.put("manifests/abc/manifest.json.enc", b"new bytes")
        folder.vanish_at_open("manifests/abc/manifest.json.enc", reappear_after=2)

        assert folder.get("manifests/abc/manifest.json.enc", expected=True) == b"new bytes"
        assert folder.sleeps == [0.05, 0.1]

    def test_file_that_stays_gone_reads_as_vanished_after_the_bounded_retry(self, folder):
        folder.put("devices/abc.json", b"{}")
        folder.vanish_at_open("devices/abc.json")

        with pytest.raises(StorageNotFound) as caught:
            folder.get("devices/abc.json", expected=True)

        assert caught.value.vanished is True
        assert caught.value.key == "devices/abc.json"
        assert folder.sleeps == [0.05, 0.1, 0.2]
        assert sum(folder.sleeps) < 0.5

    def test_key_never_seen_reads_missing_at_once(self, folder):
        with pytest.raises(StorageNotFound) as caught:
            folder.get("data/abc/" + "a" * 64 + ".enc")

        assert caught.value.vanished is False
        assert folder.sleeps == []

    def test_not_found_is_still_a_storage_error_with_the_old_message(self, folder):
        with pytest.raises(StorageError, match="file not found — data/x"):
            folder.get("data/x")

    def test_swap_before_the_open_reads_the_new_bytes(self, folder):
        folder.put("manifests/abc/manifest.json.enc", b"old")
        folder.swap_at_open("manifests/abc/manifest.json.enc", b"new")

        assert folder.get("manifests/abc/manifest.json.enc", expected=True) == b"new"
        assert folder.sleeps == []

    def test_retry_budget_is_per_backend_and_stops_sleeping_when_spent(self, folder):
        for i in range(12):
            key = f"devices/d{i}.json"
            folder.put(key, b"{}")
            folder.vanish_at_open(key)
            with pytest.raises(StorageNotFound):
                folder.get(key, expected=True)

        assert sum(folder.sleeps) <= 2.0
        spent = len(folder.sleeps)
        folder.put("devices/late.json", b"{}")
        folder.vanish_at_open("devices/late.json")
        with pytest.raises(StorageNotFound) as caught:
            folder.get("devices/late.json", expected=True)
        assert caught.value.vanished is True
        assert len(folder.sleeps) == spent  # spent budget: missing without sleeping

    def test_only_file_not_found_reads_as_missing(self, folder):
        folder.put("manifests/abc", b"a file where a directory is expected")

        with pytest.raises(StorageError) as caught:
            folder.get("manifests/abc/manifest.json.enc", expected=True)

        assert not isinstance(caught.value, StorageNotFound)
        assert folder.sleeps == []

    def test_permission_failure_is_a_failure_not_missing(self, folder):
        folder.put("data/abc/locked.enc", b"secret")
        path = folder.root / "data/abc/locked.enc"
        path.chmod(0)
        try:
            if os.access(path, os.R_OK):  # e.g. running as root
                pytest.skip("file mode is not enforced for this user")
            with pytest.raises(StorageError) as caught:
                folder.get("data/abc/locked.enc", expected=True)
            assert not isinstance(caught.value, StorageNotFound)
            assert folder.sleeps == []
        finally:
            path.chmod(0o600)

    def test_removal_after_listing_is_a_vanished_read(self, folder):
        folder.put("devices/abc.json", b"{}")
        folder.vanish_after_listing("devices/abc.json")

        keys = folder.list_keys("devices/")
        assert keys == ["devices/abc.json"]
        with pytest.raises(StorageNotFound) as caught:
            folder.get(keys[0], expected=True)
        assert caught.value.vanished is True
