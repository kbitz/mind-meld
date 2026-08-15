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
from mind_meld import skill_link

# This file owns its own path isolation: it moves $HOME deliberately, because
# it is testing the installer's real path resolution. conftest's autouse
# _isolate_skill_links pins SKILL_ROOTS to absolute tmp paths, which would
# fight that -- the marker tells it to step aside.
pytestmark = pytest.mark.owns_skill_paths


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
def codex_target(_isolate_paths):
    skills_dir = _isolate_paths / ".codex" / "skills"
    skills_dir.mkdir(parents=True)
    return skills_dir / "retro-fleet"


@pytest.fixture
def opencode_target(_isolate_paths):
    skills_dir = _isolate_paths / ".config" / "opencode" / "skills"
    skills_dir.mkdir(parents=True)
    return skills_dir / "retro-fleet"


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
    monkeypatch.setattr(skill_link, "_resolve_retro_skill_src", lambda: src)
    return src


# ---------------------------------------------------------------------------
# Branch 1: target absent → creates symlink.
# ---------------------------------------------------------------------------


class TestTargetAbsent:
    def test_creates_symlink_when_target_absent(self, target, skill_src, config_dir):
        skill_link._ensure_retro_skill_link()
        assert target.is_symlink()
        assert target.resolve() == skill_src.resolve()
        # Success marker touched.
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        assert marker.exists()

    def test_creates_codex_symlink_when_target_absent(self, codex_target, skill_src, config_dir):
        skill_link._ensure_codex_retro_skill_link()
        assert codex_target.is_symlink()
        assert codex_target.resolve() == skill_src.resolve()
        marker = config_dir / f".{skill_link._CODEX_SKILL_LINK_SUCCESS_MARKER}"
        assert marker.exists()

    def test_creates_opencode_symlink_when_target_absent(
        self, opencode_target, skill_src, config_dir
    ):
        skill_link._ensure_opencode_retro_skill_link()
        assert opencode_target.is_symlink()
        assert opencode_target.resolve() == skill_src.resolve()
        marker = config_dir / f".{skill_link._OPENCODE_SKILL_LINK_SUCCESS_MARKER}"
        assert marker.exists()

    @pytest.mark.parametrize("agent_root", [".claude", ".codex", ".config/opencode"])
    def test_creates_missing_skills_directory_when_agent_is_installed(
        self, _isolate_paths, skill_src, agent_root
    ):
        import shutil

        skills_dir = _isolate_paths / agent_root / "skills"
        if skills_dir.exists():
            shutil.rmtree(skills_dir)
        skills_dir.parent.mkdir(parents=True, exist_ok=True)

        target = skills_dir / "retro-fleet"
        if agent_root == ".claude":
            skill_link._ensure_retro_skill_link()
        elif agent_root == ".codex":
            skill_link._ensure_codex_retro_skill_link()
        else:
            skill_link._ensure_opencode_retro_skill_link()

        assert target.is_symlink()
        assert target.resolve() == skill_src.resolve()


# ---------------------------------------------------------------------------
# Branch 2: target is correct symlink → no-op.
# ---------------------------------------------------------------------------


class TestTargetCorrect:
    def test_correct_symlink_is_noop(self, target, skill_src, config_dir):
        target.symlink_to(skill_src)
        skill_link._ensure_retro_skill_link()
        # Symlink unchanged.
        assert target.is_symlink()
        assert target.resolve() == skill_src.resolve()
        # Success marker touched.
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
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
        skill_link._ensure_retro_skill_link()

        assert target.is_symlink()
        assert target.exists()  # No longer dangling.
        assert target.resolve() == skill_src.resolve()


# ---------------------------------------------------------------------------
# Branch 4: target is a real file → conflict-skip.
# ---------------------------------------------------------------------------


class TestConflictSkip:
    def test_real_file_at_target_not_clobbered(self, target, skill_src, config_dir, capsys):
        target.write_text("user's own retro-fleet skill")
        skill_link._ensure_retro_skill_link()
        # File untouched.
        assert target.read_text() == "user's own retro-fleet skill"
        assert not target.is_symlink()
        # Conflict marker touched (per cross-model #3 two-marker gate).
        conflict_marker = config_dir / f".{skill_link._SKILL_LINK_CONFLICT_MARKER}"
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
        skill_link._ensure_retro_skill_link()
        # Symlink unchanged.
        assert target.resolve() == their_dir.resolve()
        captured = capsys.readouterr()
        assert "mm: notice:" in captured.err

    def test_conflict_notice_suppressed_within_24h(self, target, skill_src, config_dir, capsys):
        """cross-model #3: per-push spam on conflict is hostile. Two-marker
        gate suppresses the notice within 24h."""
        target.write_text("user's file")
        # First call — emits notice + touches conflict marker.
        skill_link._ensure_retro_skill_link()
        first = capsys.readouterr()
        assert "mm: notice:" in first.err
        # Second call within the TTL → marker is fresh, no notice.
        skill_link._ensure_retro_skill_link()
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
        skill_link._ensure_retro_skill_link()
        captured = capsys.readouterr()
        assert captured.err == ""
        # No marker touched — next call still due.
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
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
        skill_link._ensure_retro_skill_link()  # Must not raise.
        captured = capsys.readouterr()
        assert "mm: notice:" in captured.err
        assert "PermissionError" in captured.err
        # Neither marker touched (transient failure → next push retries).
        assert not (config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}").exists()


# ---------------------------------------------------------------------------
# 24h-TTL gate behavior.
# ---------------------------------------------------------------------------


class TestSkillLinkCheckDue:
    def test_no_marker_means_check_due(self, config_dir):
        assert skill_link._skill_link_check_due() is True

    def test_codex_fresh_marker_with_correct_link_means_not_due(
        self, codex_target, skill_src, config_dir
    ):
        codex_target.symlink_to(skill_src)
        marker = config_dir / f".{skill_link._CODEX_SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        assert skill_link._codex_skill_link_check_due() is False

    def test_combined_gate_repairs_codex_when_claude_is_healthy(
        self, target, codex_target, skill_src, config_dir
    ):
        """A fresh Claude marker must not suppress an independently stale Codex link."""
        target.symlink_to(skill_src)
        codex_target.symlink_to(skill_src)
        (config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}").touch()
        (config_dir / f".{skill_link._CODEX_SKILL_LINK_SUCCESS_MARKER}").touch()
        assert skill_link._skill_links_check_due() is False

        codex_target.unlink()
        assert skill_link._skill_links_check_due() is True

    def test_combined_gate_repairs_opencode_when_other_agents_are_healthy(
        self, target, codex_target, opencode_target, skill_src, config_dir
    ):
        """Fresh Claude/Codex markers must not suppress stale OpenCode repair."""
        target.symlink_to(skill_src)
        codex_target.symlink_to(skill_src)
        opencode_target.symlink_to(skill_src)
        (config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}").touch()
        (config_dir / f".{skill_link._CODEX_SKILL_LINK_SUCCESS_MARKER}").touch()
        (config_dir / f".{skill_link._OPENCODE_SKILL_LINK_SUCCESS_MARKER}").touch()
        assert skill_link._skill_links_check_due() is False

        opencode_target.unlink()
        assert skill_link._skill_links_check_due() is True

    def test_fresh_marker_with_correct_link_means_not_due(self, target, skill_src, config_dir):
        """Steady state: marker fresh AND link points at our source → skip.
        Both conditions are required post-drift-check."""
        target.symlink_to(skill_src)
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        assert skill_link._skill_link_check_due() is False

    def test_stale_marker_means_due(self, target, skill_src, config_dir):
        target.symlink_to(skill_src)
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        old = time.time() - (25 * 3600)
        os.utime(marker, (old, old))
        assert skill_link._skill_link_check_due() is True

    def test_fresh_marker_but_link_missing_means_due(self, skill_src, config_dir):
        """REGRESSION pin for the post-cleanup-recovery bug: marker got
        touched on a previous push but the link was later removed by hand
        (e.g. user cleaning up an old workspace path). Pre-fix the fresh
        marker silently suppressed self-heal for 24h."""
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        # No symlink at target. Drift check must trip.
        assert skill_link._skill_link_check_due() is True

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
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        assert skill_link._skill_link_check_due() is True

    def test_fresh_marker_but_link_wrong_target_means_due(
        self, target, skill_src, _isolate_paths, config_dir
    ):
        """Marker fresh, link points at a different (still-extant) dir.
        E.g. user manually pointed retro-fleet at their own skill copy."""
        their_dir = _isolate_paths / "their-skill"
        their_dir.mkdir()
        target.symlink_to(their_dir)
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        assert skill_link._skill_link_check_due() is True

    def test_drift_check_resolver_failure_fails_open(self, target, config_dir, monkeypatch):
        """If ``_resolve_retro_skill_src`` raises during the drift check,
        the gate fails open (returns True) so the installer runs and
        emits its own forensic notice."""
        target_dir = target.parent / "anything"
        target_dir.mkdir()
        target.symlink_to(target_dir)
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()

        def boom():
            raise RuntimeError("resolver simulated failure")

        monkeypatch.setattr(skill_link, "_resolve_retro_skill_src", boom)
        assert skill_link._skill_link_check_due() is True

    def test_marker_stat_failure_fails_open(self, config_dir, monkeypatch):
        """TODO#3 critical-gap fix: EACCES / EIO on the marker dir must
        fail-open (treat as if no marker — re-run installer). Pre-fix the
        bare os.stat would crash push."""
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()

        from pathlib import Path

        original_stat = Path.stat

        def fake_stat(self, *args, **kwargs):
            if self.name.startswith(".skill-link"):
                raise PermissionError("simulated EACCES")
            return original_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fake_stat)
        assert skill_link._skill_link_check_due() is True


# ---------------------------------------------------------------------------
# dry_run preview contract.
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_is_noop(self, target, skill_src, config_dir):
        """`mm push --dry-run` must NOT mutate the symlink or the marker."""
        skill_link._ensure_retro_skill_link(dry_run=True)
        assert not target.exists()
        assert not (config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}").exists()


# ---------------------------------------------------------------------------
# Track 16A NEW: `skill_targets()` — the single source of truth the six
# installer wrappers and `install_skills_cmd` must agree on.
# ---------------------------------------------------------------------------


class TestSkillTargets:
    """`install_skills_cmd` used to rebuild this tuple from its own hardcoded
    literals 3,000 lines from the installer, so the two could drift silently.
    After the Track 16A cut they live in different FILES under different Group
    17 owners, which makes the drift cheaper to introduce and harder to see.
    Nothing pinned the agreement, so pin it here."""

    def test_skill_targets_are_exactly_the_installer_targets(
        self, target, codex_target, opencode_target
    ):
        """Claude / Codex / OpenCode order, matching this file's fixtures."""
        assert skill_link.skill_targets() == (target, codex_target, opencode_target)

    def test_each_wrapper_installs_at_its_skill_targets_entry(
        self, target, codex_target, opencode_target, skill_src
    ):
        """The stronger form: drive the real installers and confirm the links
        land exactly where ``skill_targets()`` says they will.

        Comparing tuples alone would still pass if BOTH sides drifted together
        (e.g. someone renames ``SKILL_ROOTS[1]`` to ``~/.codex2/skills``);
        asserting on the on-disk result is what makes that visible.
        """
        skill_link._ensure_retro_skill_links()
        assert [t for t in skill_link.skill_targets() if t.is_symlink()] == [
            target,
            codex_target,
            opencode_target,
        ]

    def test_skill_targets_are_re_resolved_per_call(self, _isolate_paths, monkeypatch, tmp_path):
        """``expanduser()`` must read ``$HOME`` at CALL time, not import time.

        A "simplification" to a module-level constant (the shape
        ``config.CONFIG_DIR`` already has, and the exact hazard the module
        docstring warns about) would freeze these at the developer's real home
        and silently defeat every isolation fixture in the suite.
        """
        before = skill_link.skill_targets()
        moved = tmp_path / "moved-home"
        moved.mkdir()
        monkeypatch.setenv("HOME", str(moved))
        after = skill_link.skill_targets()
        assert after != before
        assert all(str(t).startswith(str(moved)) for t in after), after


# ---------------------------------------------------------------------------
# Track 16A NEW: `classify_targets()` — extracted out of `install_skills_cmd`
# so Track 17A can add the missing third bucket without touching the shell.
# ---------------------------------------------------------------------------


class TestClassifyTargets:
    """Four of the five branches were only ever reached transitively through
    `mm install-skills`, and the absent-target branch — the one Track 17A is
    chartered to change — was asserted nowhere at all. It is a pure function;
    test it as one."""

    def test_correct_symlink_is_installed(self, target, skill_src):
        target.symlink_to(skill_src)
        installed, conflicts = skill_link.classify_targets((target,), skill_src)
        assert (installed, conflicts) == ([target], [])

    def test_symlink_to_elsewhere_is_a_conflict(self, target, skill_src, _isolate_paths):
        theirs = _isolate_paths / "their-skill"
        theirs.mkdir()
        target.symlink_to(theirs)
        installed, conflicts = skill_link.classify_targets((target,), skill_src)
        assert (installed, conflicts) == ([], [target])

    def test_real_file_is_a_conflict(self, target, skill_src):
        target.write_text("user's own retro-fleet skill")
        installed, conflicts = skill_link.classify_targets((target,), skill_src)
        assert (installed, conflicts) == ([], [target])

    def test_dangling_symlink_is_a_conflict(self, target, skill_src, _isolate_paths):
        """`exists()` is False on a dangling link, so the installed branch is
        skipped; the `or target.is_symlink()` tail is the only thing that keeps
        it out of the silently-ignored bucket. Reachable in the wild whenever
        the preceding self-heal's `symlink_to` failed (read-only ~/.claude)."""
        gone = _isolate_paths / "old-venv" / "retro_fleet"
        gone.mkdir(parents=True)
        target.symlink_to(gone)
        import shutil

        shutil.rmtree(gone.parent)
        assert target.is_symlink() and not target.exists()
        installed, conflicts = skill_link.classify_targets((target,), skill_src)
        assert (installed, conflicts) == ([], [target])

    def test_absent_target_is_in_neither_bucket(self, target, skill_src):
        """The documented missing third bucket. `mm install-skills` reports
        "Installed" / "conflict" off these two lists, so a target that simply
        was not created is silently reported as neither — the gap Track 17A
        owns. Pin the CURRENT behavior so 17A's change is a visible diff, not
        an accident."""
        assert not target.exists()
        assert skill_link.classify_targets((target,), skill_src) == ([], [])

    def test_resolve_failure_falls_through_to_conflict(self, target, skill_src, monkeypatch):
        """`resolve()` can raise on a path with permission issues. The
        `except OSError: pass` must fall through to conflict-skip, never
        report the link as installed."""
        import pathlib

        target.symlink_to(skill_src)
        original = pathlib.Path.resolve

        def fake_resolve(self, *a, **kw):
            if str(self) == str(target):
                raise OSError("EACCES")
            return original(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "resolve", fake_resolve)
        installed, conflicts = skill_link.classify_targets((target,), skill_src)
        assert (installed, conflicts) == ([], [target])


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
        assert "no Claude Code, Codex, or OpenCode skills directory exists" in result.output

    def test_installs_when_only_codex_skills_dir_exists(
        self, target, codex_target, skill_src, _isolate_paths
    ):
        import shutil

        from mind_meld.cli import app

        shutil.rmtree(_isolate_paths / ".claude")
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert not target.exists()
        assert codex_target.is_symlink()
        assert codex_target.resolve() == skill_src.resolve()
        assert str(codex_target) in result.output

    def test_installs_when_only_opencode_skills_dir_exists(
        self, target, codex_target, opencode_target, skill_src, _isolate_paths
    ):
        import shutil

        from mind_meld.cli import app

        shutil.rmtree(_isolate_paths / ".claude")
        shutil.rmtree(_isolate_paths / ".codex")
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert not target.exists()
        assert not codex_target.exists()
        assert opencode_target.is_symlink()
        assert opencode_target.resolve() == skill_src.resolve()
        assert str(opencode_target) in result.output

    def test_installs_when_opencode_root_exists_without_skills_directory(
        self, target, codex_target, opencode_target, skill_src, _isolate_paths
    ):
        import shutil

        from mind_meld.cli import app

        shutil.rmtree(_isolate_paths / ".claude")
        shutil.rmtree(_isolate_paths / ".codex")
        shutil.rmtree(opencode_target.parent)
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert not target.exists()
        assert not codex_target.exists()
        assert opencode_target.is_symlink()
        assert opencode_target.resolve() == skill_src.resolve()

    def test_reports_conflict_without_undoing_codex_install(self, target, codex_target, skill_src):
        from mind_meld.cli import app

        target.write_text("user's own retro-fleet")
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 1
        assert target.read_text() == "user's own retro-fleet"
        assert codex_target.is_symlink()
        assert codex_target.resolve() == skill_src.resolve()
        assert str(codex_target) in result.output
        assert str(target) in result.output

    def test_bypasses_ttl_gate(self, target, skill_src, config_dir):
        """The CLI command runs the installer regardless of the 24h-TTL
        marker. The gate only governs the implicit self-heal in push."""
        from mind_meld.cli import app

        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()  # fresh — would suppress push-time self-heal
        # Link is still missing; the command must create it anyway.
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert target.is_symlink()


class TestPushSkillLinkWiring:
    def test_push_uses_combined_gate_and_plural_installer(self, monkeypatch):
        """Push self-heals both agents before its no-sources early return."""
        calls: list[bool] = []
        config = {
            "device": {"id": "dev-a", "name": "Mac A"},
            "sync": {"max_file_size": 1024},
        }
        monkeypatch.setattr(cli_module, "get_backend", lambda _config: object())
        monkeypatch.setattr(cli_module, "_ensure_device_registered", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(skill_link, "_skill_links_check_due", lambda: True)
        monkeypatch.setattr(
            skill_link,
            "_ensure_retro_skill_links",
            lambda *, dry_run: calls.append(dry_run),
        )
        monkeypatch.setattr(cli_module, "get_sources", lambda _config: [])

        assert cli_module._push_core(config, "pw", 1024) is None
        assert calls == [False]
