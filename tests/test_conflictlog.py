"""Tests for the TEMPORARY conflict-decision collector (conflictlog.py).

Scope is deliberately light (disposable telemetry) EXCEPT the never-raises
contract, which is load-bearing because append_decision is called from inside
the `mm resolve` walk and a raise there would break a user's resolution.
"""

import difflib

from mind_meld import conflictlog
from mind_meld.merge import lcs_merge, similarity_ratio


class TestAppendDecision:
    def test_writes_wellformed_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr(conflictlog, "LOG_DIR", tmp_path)
        ok = conflictlog.append_decision(
            device="dev1", source="claude", rel_path="memory/x.md", site="resolve",
            mode="post_inversion", choice="local", via="typed", outcome="resolved",
            similarity=0.9, merge_conflicts=0, binary=False,
        )
        assert ok is True
        rows = list(conflictlog.read_records())
        assert len(rows) == 1
        row = rows[0]
        assert row["schema"] == conflictlog.SCHEMA
        assert row["choice"] == "local"
        assert row["site"] == "resolve"
        assert row["similarity"] == 0.9
        assert "ts" in row  # stamped

    def test_append_is_mode_0600(self, tmp_path, monkeypatch):
        monkeypatch.setattr(conflictlog, "LOG_DIR", tmp_path)
        conflictlog.append_decision(device="d", source="s", rel_path="p", site="resolve")
        mode = conflictlog.log_path().stat().st_mode & 0o777
        assert mode == 0o600

    def test_never_raises_and_returns_false_on_oserror(self, tmp_path, monkeypatch):
        # Force the write to fail — append_decision must swallow it and return
        # False, never propagate into the caller's resolve loop.
        monkeypatch.setattr(conflictlog, "LOG_DIR", tmp_path)

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(conflictlog.os, "open", _boom)
        assert conflictlog.append_decision(device="d", source="s", rel_path="p") is False

    def test_never_raises_on_nonserializable_field(self, tmp_path, monkeypatch):
        monkeypatch.setattr(conflictlog, "LOG_DIR", tmp_path)
        assert conflictlog.append_decision(bad=object()) is False
        assert list(conflictlog.read_records()) == []

    def test_multiple_appends_accumulate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(conflictlog, "LOG_DIR", tmp_path)
        for i in range(3):
            conflictlog.append_decision(device="d", source="s", rel_path=f"p{i}", choice="skip")
        assert len(list(conflictlog.read_records())) == 3


class TestReadRecords:
    def test_skips_corrupt_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(conflictlog, "LOG_DIR", tmp_path)
        p = conflictlog.log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"schema":1,"choice":"local"}\nnot json\n\n{"schema":1,"choice":"remote"}\n')
        rows = list(conflictlog.read_records())
        assert [r["choice"] for r in rows] == ["local", "remote"]

    def test_missing_file_yields_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(conflictlog, "LOG_DIR", tmp_path)
        assert list(conflictlog.read_records()) == []


class TestSimilarityParity:
    """similarity_ratio MUST match the representation lcs_merge uses, or the
    collected dataset can't validate the Phase 2 classifier's thresholds (E2)."""

    def _ratio(self, local: bytes, remote: bytes) -> float:
        a = local.decode("utf-8").splitlines()
        b = remote.decode("utf-8").splitlines()
        return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()

    def test_identical_is_one(self):
        data = b"alpha\nbeta\ngamma\ndelta\n"
        assert similarity_ratio(data, data) == 1.0

    def test_nul_byte_is_none_like_lcs_merge(self):
        assert similarity_ratio(b"a\x00b", b"c") is None
        assert lcs_merge(b"a\x00b", b"c")[1] == -1  # parity with the -1 sentinel

    def test_invalid_utf8_is_none_like_lcs_merge(self):
        bad = b"\xff\xfe\xfa"
        assert similarity_ratio(bad, b"text") is None
        assert lcs_merge(bad, b"text")[1] == -1

    def test_trailing_newline_variation_ignored(self):
        # splitlines() without keepends → identical lines → ratio 1.0
        assert similarity_ratio(b"a\nb\nc", b"a\nb\nc\n") == 1.0

    def test_matches_raw_sequencematcher_on_repetitive_input(self):
        # >200 repetitive lines: the exact autojunk/splitlines rules matter.
        local = ("line\n" * 300).encode("utf-8")
        remote = ("line\n" * 250 + "diff\n" * 50).encode("utf-8")
        assert similarity_ratio(local, remote) == self._ratio(local, remote)

    def test_empty_both_is_one(self):
        assert similarity_ratio(b"", b"") == 1.0


class TestResolveHookEmitsRow:
    """End-to-end: the `mm resolve` walk emits one labeled row per decision.
    conftest's _isolate_conflictlog redirects LOG_DIR to a per-test path."""

    def test_keep_local_emits_labeled_row(self, tmp_path, monkeypatch):
        import typer

        from mind_meld.cli import _resolve_interactive_loop

        canonical = tmp_path / "user.md"
        canonical.write_bytes(b"alpha\nbeta\ngamma\n")  # post-inversion: canonical = local
        conflict = tmp_path / "user.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"alpha\nBETA\ngamma\n")  # sidecar = remote
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "l")

        _resolve_interactive_loop([("s1", conflict, canonical)], device_id="mydev")

        rows = list(conflictlog.read_records())
        assert len(rows) == 1
        row = rows[0]
        assert row["site"] == "resolve"
        assert row["choice"] == "local"
        assert row["via"] == "typed"
        assert row["outcome"] == "resolved"
        assert row["mode"] == "post_inversion"
        assert row["device"] == "mydev"
        assert row["binary"] is False
        assert row["merge_conflicts"] == 1  # the single replaced line (beta/BETA)
        assert 0.0 <= row["similarity"] <= 1.0
        assert row["local_sha"] and row["remote_sha"]
        assert row["peer_short"] == "devA1234"

    def test_skip_records_skipped_outcome(self, tmp_path, monkeypatch):
        import typer

        from mind_meld.cli import _resolve_interactive_loop

        canonical = tmp_path / "user.md"
        canonical.write_bytes(b"local content")
        conflict = tmp_path / "user.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"remote content")
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "s")

        _resolve_interactive_loop([("s1", conflict, canonical)], device_id="mydev")

        row = list(conflictlog.read_records())[0]
        assert row["choice"] == "skip"
        assert row["outcome"] == "skipped"

    def test_canonical_missing_records_mode(self, tmp_path, monkeypatch):
        import typer

        from mind_meld.cli import _resolve_interactive_loop

        conflict = tmp_path / "user.sync-conflict-20260421-143055-devA1234.md"
        conflict.write_bytes(b"orphan remote bytes")
        monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "s")

        _resolve_interactive_loop([("s1", conflict, None)], device_id="mydev")

        row = list(conflictlog.read_records())[0]
        assert row["mode"] == "canonical_missing"
        assert row["choice"] == "skip"
        assert row["outcome"] == "skipped"
        assert "similarity" not in row  # no two-file pair → features absent
