"""Group 8 / Track 8A: ``_ensure_retro_skill_link`` symlink installer pins.

Pins every branch identified in the /plan-eng-review coverage diagram:

* target absent → creates symlink + touches success marker
* target = correct symlink → no-op + touches success marker
* target = dangling symlink (pipx-reinstall recovery) — IRON RULE
  REGRESSION pin from Test review #1
* target = real file (user's own) → conflict-skip + conflict marker
* target = symlink to wrong location → conflict-skip + conflict marker
* ~/.claude/skills doesn't exist → silent skip
* ``symlink_to`` raises OSError → mm: notice: + continue (CQ#1 contract)
* TOCTOU race FileExistsError → mm: notice: + continue
* 24h-TTL gate fresh → ``_skill_link_check_due`` returns False
* 24h-TTL gate stale → returns True
* gate stat fails (EACCES) → fail-open (TODO#3 critical-gap fix)
* dry_run=True → installer is a no-op
* Conflict notice not re-emitted within 24h (cross-model #3 two-marker gate)
"""

from __future__ import annotations

import os
import time

import pytest

from mind_meld import cli as cli_module


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Point all the installer's filesystem touchpoints at tmp_path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    (fake_home / ".config" / "mind-meld").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # Path("~/...").expanduser() reads HOME, so we just need to set the env.
    yield fake_home


@pytest.fixture
def target(_isolate_paths):
    return _isolate_paths / ".claude" / "skills" / "retro-fleet"


@pytest.fixture
def config_dir(_isolate_paths):
    return _isolate_paths / ".config" / "mind-meld"


@pytest.fixture
def skill_src(_isolate_paths, monkeypatch):
    """Stand-in for the wheel's mind_meld/skills/retro_fleet/ dir."""
    src = _isolate_paths / "wheel" / "skills" / "retro_fleet"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# retro-fleet")
    (src / "aggregator.py").write_text("# aggregator")
    monkeypatch.setattr(cli_module, "_resolve_retro_skill_src", lambda: src)
    return src


# ---------------------------------------------------------------------------
# Branch 1: target absent → creates symlink.
# ---------------------------------------------------------------------------


class TestTargetAbsent:
    def test_creates_symlink_when_target_absent(self, target, skill_src, config_dir):
        cli_module._ensure_retro_skill_link()
        assert target.is_symlink()
        assert target.resolve() == skill_src.resolve()
        # Success marker touched.
        marker = config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}"
        assert marker.exists()


# ---------------------------------------------------------------------------
# Branch 2: target is correct symlink → no-op.
# ---------------------------------------------------------------------------


class TestTargetCorrect:
    def test_correct_symlink_is_noop(self, target, skill_src, config_dir):
        target.symlink_to(skill_src)
        cli_module._ensure_retro_skill_link()
        # Symlink unchanged.
        assert target.is_symlink()
        assert target.resolve() == skill_src.resolve()
        # Success marker touched.
        marker = config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}"
        assert marker.exists()


# ---------------------------------------------------------------------------
# Branch 3: dangling symlink (pipx-reinstall recovery) — IRON RULE PIN.
# ---------------------------------------------------------------------------


class TestDanglingSymlink:
    """REGRESSION test for /plan-eng-review Test review #1.

    Pre-fix, the original installer pseudocode never replaced a dangling
    symlink because is_symlink() returned True and the conflict-skip
    branch matched. This locked pipx-reinstall recovery into a permanent
    broken state.
    """

    def test_dangling_symlink_unlinked_and_recreated(self, target, skill_src, _isolate_paths):
        """Symlink whose target no longer exists → unlink + recreate."""
        # Create a deleted target (simulates pipx reinstall rewriting the
        # venv path).
        deleted_target = _isolate_paths / "old-venv" / "skills" / "retro_fleet"
        deleted_target.mkdir(parents=True)
        target.symlink_to(deleted_target)
        # Now delete the target — symlink dangles.
        import shutil

        shutil.rmtree(deleted_target.parent.parent)
        assert target.is_symlink()
        assert not target.exists()  # dangling

        # Self-heal must replace it pointing at the live source.
        cli_module._ensure_retro_skill_link()

        assert target.is_symlink()
        assert target.exists()  # No longer dangling.
        assert target.resolve() == skill_src.resolve()


# ---------------------------------------------------------------------------
# Branch 4: target is a real file → conflict-skip.
# ---------------------------------------------------------------------------


class TestConflictSkip:
    def test_real_file_at_target_not_clobbered(self, target, skill_src, config_dir, capsys):
        target.write_text("user's own retro-fleet skill")
        cli_module._ensure_retro_skill_link()
        # File untouched.
        assert target.read_text() == "user's own retro-fleet skill"
        assert not target.is_symlink()
        # Conflict marker touched (per cross-model #3 two-marker gate).
        conflict_marker = config_dir / f".{cli_module._SKILL_LINK_CONFLICT_MARKER}"
        assert conflict_marker.exists()
        # Notice emitted.
        captured = capsys.readouterr()
        assert "mm: notice:" in captured.err
        assert "exists" in captured.err

    def test_wrong_symlink_target_not_clobbered(self, target, skill_src, _isolate_paths, capsys):
        # User pointed retro-fleet at their own skill dir.
        their_dir = _isolate_paths / "their-skill"
        their_dir.mkdir()
        target.symlink_to(their_dir)
        cli_module._ensure_retro_skill_link()
        # Symlink unchanged.
        assert target.resolve() == their_dir.resolve()
        captured = capsys.readouterr()
        assert "mm: notice:" in captured.err

    def test_conflict_notice_suppressed_within_24h(self, target, skill_src, config_dir, capsys):
        """cross-model #3: per-push spam on conflict is hostile. Two-marker
        gate suppresses the notice within 24h."""
        target.write_text("user's file")
        # First call — emits notice + touches conflict marker.
        cli_module._ensure_retro_skill_link()
        first = capsys.readouterr()
        assert "mm: notice:" in first.err
        # Second call within the TTL → marker is fresh, no notice.
        cli_module._ensure_retro_skill_link()
        second = capsys.readouterr()
        assert second.err == ""


# ---------------------------------------------------------------------------
# Branch 5: ~/.claude/skills/ doesn't exist → silent skip.
# ---------------------------------------------------------------------------


class TestNoClaudeCode:
    def test_skills_dir_absent_silent_skip(self, _isolate_paths, skill_src, config_dir, capsys):
        """Fresh Mac without Claude Code → installer no-ops silently.
        Important: also doesn't touch the success marker — if the user
        later installs Claude Code, the next push must re-evaluate."""
        # Remove ~/.claude/skills entirely.
        import shutil

        shutil.rmtree(_isolate_paths / ".claude")
        cli_module._ensure_retro_skill_link()
        captured = capsys.readouterr()
        assert captured.err == ""
        # No marker touched — next call still due.
        marker = config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}"
        assert not marker.exists()


# ---------------------------------------------------------------------------
# Branch 6: symlink_to raises OSError → forensic-only.
# ---------------------------------------------------------------------------


class TestSymlinkToError:
    def test_oserror_during_symlink_creates_notice_no_crash(
        self, target, skill_src, config_dir, capsys, monkeypatch
    ):
        """CQ#1 contract: TOCTOU FileExistsError, EACCES, EPERM, ENOTSUP
        on symlink_to → emit notice, return cleanly. Push must not crash."""
        import pathlib

        original_symlink_to = pathlib.Path.symlink_to

        def fake_symlink_to(self, target_path, target_is_directory=False):
            if self.name == "retro-fleet":
                raise PermissionError("simulated read-only ~/.claude")
            return original_symlink_to(self, target_path, target_is_directory)

        monkeypatch.setattr(pathlib.Path, "symlink_to", fake_symlink_to)
        cli_module._ensure_retro_skill_link()  # Must not raise.
        captured = capsys.readouterr()
        assert "mm: notice:" in captured.err
        assert "PermissionError" in captured.err
        # Neither marker touched (transient failure → next push retries).
        assert not (config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}").exists()


# ---------------------------------------------------------------------------
# 24h-TTL gate behavior.
# ---------------------------------------------------------------------------


class TestSkillLinkCheckDue:
    def test_no_marker_means_check_due(self, config_dir):
        assert cli_module._skill_link_check_due() is True

    def test_fresh_marker_with_correct_link_means_not_due(self, target, skill_src, config_dir):
        """Steady state: marker fresh AND link points at our source → skip.
        Both conditions are required post-drift-check."""
        target.symlink_to(skill_src)
        marker = config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        assert cli_module._skill_link_check_due() is False

    def test_stale_marker_means_due(self, target, skill_src, config_dir):
        target.symlink_to(skill_src)
        marker = config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        old = time.time() - (25 * 3600)
        os.utime(marker, (old, old))
        assert cli_module._skill_link_check_due() is True

    def test_fresh_marker_but_link_missing_means_due(self, skill_src, config_dir):
        """REGRESSION pin for the post-cleanup-recovery bug: marker got
        touched on a previous push but the link was later removed by hand
        (e.g. user cleaning up an old workspace path). Pre-fix the fresh
        marker silently suppressed self-heal for 24h."""
        marker = config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        # No symlink at target. Drift check must trip.
        assert cli_module._skill_link_check_due() is True

    def test_fresh_marker_but_link_dangling_means_due(
        self, target, skill_src, _isolate_paths, config_dir
    ):
        """Marker fresh, link points at a directory that no longer exists
        (mirrors the dangling-symlink IRON-RULE case from TestDanglingSymlink
        but at the gate level so push self-heals on the next call)."""
        deleted_target = _isolate_paths / "old-venv" / "skills" / "retro_fleet"
        deleted_target.mkdir(parents=True)
        target.symlink_to(deleted_target)
        import shutil

        shutil.rmtree(deleted_target.parent.parent)
        marker = config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        assert cli_module._skill_link_check_due() is True

    def test_fresh_marker_but_link_wrong_target_means_due(
        self, target, skill_src, _isolate_paths, config_dir
    ):
        """Marker fresh, link points at a different (still-extant) dir.
        E.g. user manually pointed retro-fleet at their own skill copy."""
        their_dir = _isolate_paths / "their-skill"
        their_dir.mkdir()
        target.symlink_to(their_dir)
        marker = config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        assert cli_module._skill_link_check_due() is True

    def test_drift_check_resolver_failure_fails_open(self, target, config_dir, monkeypatch):
        """If ``_resolve_retro_skill_src`` raises during the drift check,
        the gate fails open (returns True) so the installer runs and
        emits its own forensic notice."""
        target_dir = target.parent / "anything"
        target_dir.mkdir()
        target.symlink_to(target_dir)
        marker = config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()

        def boom():
            raise RuntimeError("resolver simulated failure")

        monkeypatch.setattr(cli_module, "_resolve_retro_skill_src", boom)
        assert cli_module._skill_link_check_due() is True

    def test_marker_stat_failure_fails_open(self, config_dir, monkeypatch):
        """TODO#3 critical-gap fix: EACCES / EIO on the marker dir must
        fail-open (treat as if no marker — re-run installer). Pre-fix the
        bare os.stat would crash push."""
        marker = config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()

        from pathlib import Path

        original_stat = Path.stat

        def fake_stat(self, *args, **kwargs):
            if self.name.startswith(".skill-link"):
                raise PermissionError("simulated EACCES")
            return original_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fake_stat)
        assert cli_module._skill_link_check_due() is True


# ---------------------------------------------------------------------------
# dry_run preview contract.
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_is_noop(self, target, skill_src, config_dir):
        """`mm push --dry-run` must NOT mutate the symlink or the marker."""
        cli_module._ensure_retro_skill_link(dry_run=True)
        assert not target.exists()
        assert not (config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}").exists()


# ---------------------------------------------------------------------------
# `mm install-skills` user-facing command.
# ---------------------------------------------------------------------------


class TestInstallSkillsCommand:
    """The user-facing companion to ``_ensure_retro_skill_link``: explicit
    install on demand, useful when the steady-state self-heal hasn't run
    yet (fresh machine) or when the user wants to force a re-install
    after manual cleanup. Bypasses the 24h TTL gate by calling the
    installer directly."""

    def _runner(self):
        from typer.testing import CliRunner

        return CliRunner()

    def test_creates_symlink_when_absent(self, target, skill_src, _isolate_paths):
        from mind_meld.cli import app

        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert "Installed" in result.output
        assert target.is_symlink()
        assert target.resolve() == skill_src.resolve()

    def test_idempotent_on_correct_link(self, target, skill_src):
        from mind_meld.cli import app

        target.symlink_to(skill_src)
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert "Installed" in result.output

    def test_self_heals_dangling_link(self, target, skill_src, _isolate_paths):
        from mind_meld.cli import app

        deleted_target = _isolate_paths / "old-venv" / "skills" / "retro_fleet"
        deleted_target.mkdir(parents=True)
        target.symlink_to(deleted_target)
        import shutil

        shutil.rmtree(deleted_target.parent.parent)

        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert target.exists()  # no longer dangling
        assert target.resolve() == skill_src.resolve()

    def test_errors_on_conflict_real_file(self, target, skill_src):
        from mind_meld.cli import app

        target.write_text("user's own retro-fleet")
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 1
        assert target.read_text() == "user's own retro-fleet"

    def test_errors_when_claude_skills_dir_missing(self, _isolate_paths, skill_src):
        import shutil

        from mind_meld.cli import app

        shutil.rmtree(_isolate_paths / ".claude")
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 1
        assert "does not exist" in result.output or "does not exist" in (result.stderr or "")

    def test_bypasses_ttl_gate(self, target, skill_src, config_dir):
        """The CLI command runs the installer regardless of the 24h-TTL
        marker. The gate only governs the implicit self-heal in push."""
        from mind_meld.cli import app

        marker = config_dir / f".{cli_module._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()  # fresh — would suppress push-time self-heal
        # Link is still missing; the command must create it anyway.
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert target.is_symlink()
