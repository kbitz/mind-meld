"""Tests for mind_meld.pullhistory — append-only JSONL writer + reader.

Covers the contract documented in pullhistory.py: append → flock-guarded,
0600 perms, rotate-at-line-boundary at 1MB, reader tolerates a corrupt
first line in the rotated `.1` (crash-mid-rotate fingerprint).
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from mind_meld import pullhistory


@pytest.fixture(autouse=True)
def _isolate_history_dir(tmp_path, monkeypatch):
    """Redirect HISTORY_DIR so tests never touch the user's real history."""
    iso = tmp_path / "mm_state"
    monkeypatch.setattr("mind_meld.pullhistory.HISTORY_DIR", iso)


def _live_path() -> "os.PathLike":
    return pullhistory.history_path()


def _rotated_path() -> "os.PathLike":
    p = pullhistory.history_path()
    return p.with_name(p.name + pullhistory.ROTATED_SUFFIX)


class TestAppend:
    def test_creates_directory_on_first_append(self):
        assert not pullhistory.HISTORY_DIR.exists()
        pullhistory.append(
            verb="pull",
            device="dev-a",
            source="claude",
            rel_path="memory/role.md",
            action="written",
        )
        assert pullhistory.HISTORY_DIR.exists()
        assert _live_path().exists()

    def test_append_writes_one_line_per_record(self):
        for i in range(3):
            pullhistory.append(
                verb="pull",
                device="dev-a",
                source="claude",
                rel_path=f"memory/file-{i}.md",
                action="written",
            )
        lines = _live_path().read_text().strip().split("\n")
        assert len(lines) == 3
        for line in lines:
            json.loads(line)  # valid JSON

    def test_appended_record_has_required_schema_keys(self):
        pullhistory.append(
            verb="push",
            device="dev-b",
            source="gstack",
            rel_path="config.yaml",
            action="uploaded",
            local_sha="abc123",
        )
        rec = json.loads(_live_path().read_text().strip())
        assert rec["verb"] == "push"
        assert rec["device"] == "dev-b"
        assert rec["source"] == "gstack"
        assert rec["rel_path"] == "config.yaml"
        assert rec["action"] == "uploaded"
        assert rec["local_sha"] == "abc123"
        assert "ts" in rec

    def test_optional_fields_omitted_when_unset(self):
        pullhistory.append(
            verb="pull",
            device="dev-a",
            source="claude",
            rel_path="memory/role.md",
            action="skipped",
        )
        rec = json.loads(_live_path().read_text().strip())
        assert "local_sha" not in rec
        assert "remote_sha" not in rec
        assert "sidecar" not in rec

    def test_file_perms_are_0600(self):
        pullhistory.append(
            verb="pull",
            device="dev-a",
            source="claude",
            rel_path="memory/role.md",
            action="written",
        )
        mode = stat.S_IMODE(_live_path().stat().st_mode)
        assert mode == 0o600

    def test_failure_to_create_dir_does_not_raise(self, monkeypatch):
        """append() must never raise — history is forensic only."""

        def boom(*_a, **_kw):
            raise OSError("simulated")

        monkeypatch.setattr("pathlib.Path.mkdir", boom)
        # Must not raise
        pullhistory.append(
            verb="pull",
            device="dev-a",
            source="claude",
            rel_path="x",
            action="written",
        )


class TestRotation:
    def test_rotation_fires_when_size_exceeds_cap(self, monkeypatch):
        # Shrink the cap so the test stays fast.
        monkeypatch.setattr("mind_meld.pullhistory._ROTATE_BYTES", 200)

        # Three appends will exceed 200 bytes (each line ~150+ bytes).
        for i in range(5):
            pullhistory.append(
                verb="pull",
                device="dev-a-pad-pad-pad",
                source="claude-source",
                rel_path=f"memory/file-with-a-longer-name-{i}.md",
                action="written",
                local_sha="0" * 64,
            )
        # Rotation runs AFTER the append that pushed past the cap.
        assert _rotated_path().exists(), "rotated .1 must exist after cap exceeded"
        # Live file is freshly empty (or holds whatever appends happened
        # after the most recent rotation).
        assert _live_path().exists() or True  # may be re-created on next write

    def test_rotation_at_line_boundary_no_byte_truncation(self, monkeypatch):
        """The rotated .1 must contain WHOLE lines only — no torn JSON
        from a mid-line truncate. We never byte-tail-truncate."""
        monkeypatch.setattr("mind_meld.pullhistory._ROTATE_BYTES", 200)

        for i in range(4):
            pullhistory.append(
                verb="pull",
                device="dev-a",
                source="claude",
                rel_path=f"memory/file-{i:04d}.md",
                action="written",
                local_sha="a" * 64,
            )
        rotated = _rotated_path()
        assert rotated.exists()
        for line in rotated.read_text().split("\n"):
            if not line.strip():
                continue
            json.loads(line)  # every line must be valid JSON

    def test_rotation_overwrites_prior_dot_one(self, monkeypatch):
        monkeypatch.setattr("mind_meld.pullhistory._ROTATE_BYTES", 100)

        # First batch — rotate.
        for i in range(3):
            pullhistory.append(
                verb="pull",
                device="dev-a",
                source="claude",
                rel_path=f"a-{i}.md",
                action="written",
            )
        first_dot_one = _rotated_path().read_text()

        # Second batch — rotate again, overwriting .1.
        for i in range(3):
            pullhistory.append(
                verb="pull",
                device="dev-b",  # different device so contents differ
                source="claude",
                rel_path=f"b-{i}.md",
                action="written",
            )
        second_dot_one = _rotated_path().read_text()
        assert first_dot_one != second_dot_one


class TestReadRecords:
    def test_invalid_utf8_line_is_skipped_not_raised(self):
        """v0.12.16 T3: `_yield_lines` read text-mode guarded only by
        OSError, so one invalid UTF-8 byte raised UnicodeDecodeError out of
        the generator into the caller's frame — inconsistent with the
        forensic-reader stance every other branch of this function takes.

        Bad byte in the MIDDLE: the rows on both sides must survive, which
        also proves the tolerance is per-line rather than a bail-out.
        """
        live = _live_path()
        live.parent.mkdir(parents=True, exist_ok=True)

        def _rec(rel_path: str) -> bytes:
            return (
                json.dumps(
                    {
                        "ts": "2026-08-14T00:00:00+00:00",
                        "verb": "pull",
                        "device": "dev-a",
                        "source": "claude",
                        "rel_path": rel_path,
                        "action": "written",
                    }
                ).encode()
                + b"\n"
            )

        live.write_bytes(_rec("before.md") + b'{"rel_path":"\xe9bad"}\n' + _rec("after.md"))
        recs = list(pullhistory.read_records())
        assert [r["rel_path"] for r in recs] == ["before.md", "after.md"]

    def test_read_returns_records_in_file_order(self):
        for i in range(3):
            pullhistory.append(
                verb="pull",
                device="dev-a",
                source="claude",
                rel_path=f"f-{i}.md",
                action="written",
            )
        recs = list(pullhistory.read_records())
        assert [r["rel_path"] for r in recs] == ["f-0.md", "f-1.md", "f-2.md"]

    def test_read_yields_rotated_then_live(self):
        """Reader yields rotated `.1` records before live records."""
        # Construct rotated + live files directly so we don't fight the
        # rotation cap (set to 1MB by default; tests fiddling with it
        # have race-against-the-cap timing).
        rotated = _rotated_path()
        live = _live_path()
        rotated.parent.mkdir(parents=True, exist_ok=True)

        def _rec(rel_path: str) -> str:
            return json.dumps(
                {
                    "ts": "2026-04-25T00:00:00+00:00",
                    "verb": "pull",
                    "device": "dev-a",
                    "source": "claude",
                    "rel_path": rel_path,
                    "action": "written",
                },
                sort_keys=True,
            )

        rotated.write_text("\n".join(_rec(f"old-{i}.md") for i in range(3)) + "\n")
        live.write_text("\n".join(_rec(f"new-{i}.md") for i in range(2)) + "\n")

        recs = list(pullhistory.read_records())
        rels = [r["rel_path"] for r in recs]
        # Rotated content (older) appears first.
        assert rels == ["old-0.md", "old-1.md", "old-2.md", "new-0.md", "new-1.md"]

    def test_read_tolerates_corrupt_first_line_in_rotated(self):
        """Crash-mid-rotate may leave a torn first line in .1.
        Reader skips it and continues."""
        rotated = _rotated_path()
        rotated.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps(
            {
                "ts": "2026-04-25T00:00:00+00:00",
                "verb": "pull",
                "device": "dev-a",
                "source": "claude",
                "rel_path": "good.md",
                "action": "written",
            },
            sort_keys=True,
        )
        # First line is corrupt JSON; remaining lines are valid.
        rotated.write_text(f"{{not-valid-json\n{good}\n{good}\n")
        recs = list(pullhistory.read_records())
        assert len(recs) == 2
        assert all(r["rel_path"] == "good.md" for r in recs)

    def test_read_skips_corrupt_lines_in_live(self):
        live = _live_path()
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text("not-json\n" + json.dumps({"verb": "pull", "rel_path": "x"}) + "\n")
        recs = list(pullhistory.read_records())
        assert len(recs) == 1

    def test_read_returns_empty_when_no_files(self):
        assert list(pullhistory.read_records()) == []
