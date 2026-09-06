"""Tests for mind_meld.storage.local — local folder backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from mind_meld import fsutil
from mind_meld.errors import StorageError
from mind_meld.storage import local as storage_local
from mind_meld.storage.local import LocalBackend


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
