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
from pathlib import Path

import pytest

from mind_meld import cli as cli_module
from mind_meld import skill_link


def _assert_store_link(target):
    store = skill_link._skill_store_dir()
    assert target.is_symlink()
    assert Path(os.readlink(target)) == store
    assert (store / "SKILL.md").is_file()
    assert not (store / "aggregator.py").exists()


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
    """Stand-in for the wheel's mind_meld/skills/retro_fleet/ dir.

    Path is package-shaped (``site-packages/mind_meld/skills/retro_fleet``)
    so a pre-B link to it is a migration candidate, not foreign.
    """
    src = (
        _isolate_paths
        / "venv"
        / "lib"
        / "python3.14"
        / "site-packages"
        / "mind_meld"
        / "skills"
        / "retro_fleet"
    )
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# retro-fleet")
    (src / "aggregator.py").write_text("# aggregator")
    monkeypatch.setattr(skill_link, "_resolve_retro_skill_src", lambda: src)
    return src


@pytest.fixture
def store(_isolate_paths):
    return _isolate_paths / ".local" / "share" / "mind-meld" / "agent-skills" / "retro-fleet"


# ---------------------------------------------------------------------------
# Branch 1: target absent → creates symlink.
# ---------------------------------------------------------------------------


class TestTargetAbsent:
    def test_creates_symlink_when_target_absent(self, target, skill_src, config_dir):
        skill_link._ensure_retro_skill_link()
        _assert_store_link(target)
        # Success marker touched.
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        assert marker.exists()

    def test_creates_codex_symlink_when_target_absent(self, codex_target, skill_src, config_dir):
        skill_link._ensure_codex_retro_skill_link()
        _assert_store_link(codex_target)
        marker = config_dir / f".{skill_link._CODEX_SKILL_LINK_SUCCESS_MARKER}"
        assert marker.exists()

    def test_creates_opencode_symlink_when_target_absent(
        self, opencode_target, skill_src, config_dir
    ):
        skill_link._ensure_opencode_retro_skill_link()
        _assert_store_link(opencode_target)
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
        _assert_store_link(target)


# ---------------------------------------------------------------------------
# Branch 2: target is correct symlink → no-op.
# ---------------------------------------------------------------------------


class TestTargetCorrect:
    def test_correct_symlink_is_noop(self, target, skill_src, config_dir):
        skill_link._ensure_retro_skill_link()
        skill_link._ensure_retro_skill_link()
        assert target.is_symlink()
        _assert_store_link(target)
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        assert marker.exists()


# ---------------------------------------------------------------------------
# Branch 3: dangling symlink is a no-clobber conflict.
# ---------------------------------------------------------------------------


class TestDanglingSymlink:
    def test_dangling_symlink_is_not_replaced(self, target, skill_src, _isolate_paths, capsys):
        """A dangling path could be replaced concurrently, so never unlink it."""
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

        # It remains a conflict until the user removes it deliberately.
        skill_link._ensure_retro_skill_link()

        assert target.is_symlink()
        assert not target.exists()
        # The notice now names the real cause per status instead of one
        # hardcoded "not replacing" for every branch. This link points at
        # old-venv/skills/retro_fleet -- no `mind_meld` component -- so
        # _legacy_shape is "other" and the installer classifies it `foreign`.
        err = capsys.readouterr().err
        assert "which is not mm's skill store" in err, err
        assert "mm install-skills" in err, err


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
        # A real file at the target is `foreign`; the notice must say so and
        # must NOT claim it is a broken mm link.
        assert "which is not mm's skill store" in captured.err, captured.err
        assert "move it aside" in captured.err, captured.err

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
        original_symlink = os.symlink

        def fake_symlink(src, dst, target_is_directory=False):
            if Path(dst).name == "retro-fleet":
                raise PermissionError("simulated read-only ~/.claude")
            return original_symlink(src, dst, target_is_directory)

        monkeypatch.setattr(os, "symlink", fake_symlink)
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

    @pytest.mark.parametrize(
        ("gate_name", "target_index", "success_marker"),
        [
            ("_skill_link_check_due", 0, skill_link._SKILL_LINK_SUCCESS_MARKER),
            ("_codex_skill_link_check_due", 1, skill_link._CODEX_SKILL_LINK_SUCCESS_MARKER),
            ("_opencode_skill_link_check_due", 2, skill_link._OPENCODE_SKILL_LINK_SUCCESS_MARKER),
        ],
    )
    def test_present_agent_root_without_skills_directory_is_due(
        self, config_dir, gate_name, target_index, success_marker
    ):
        """A fresh legacy marker must not delay repair of a missing skills dir."""
        import shutil

        skills_dir = skill_link.skill_targets()[target_index].parent
        if skills_dir.exists():
            shutil.rmtree(skills_dir)
        skills_dir.parent.mkdir(parents=True, exist_ok=True)
        (config_dir / f".{success_marker}").touch()

        assert getattr(skill_link, gate_name)() is True

    def test_codex_fresh_marker_with_correct_link_means_not_due(
        self, codex_target, skill_src, config_dir
    ):
        skill_link._ensure_codex_retro_skill_link()
        assert skill_link._codex_skill_link_check_due() is False

    def test_combined_gate_repairs_codex_when_claude_is_healthy(
        self, target, codex_target, skill_src, config_dir
    ):
        """A fresh Claude marker must not suppress an independently stale Codex link."""
        skill_link._ensure_retro_skill_links()
        assert skill_link._skill_links_check_due() is False

        codex_target.unlink()
        assert skill_link._skill_links_check_due() is True

    def test_combined_gate_repairs_opencode_when_other_agents_are_healthy(
        self, target, codex_target, opencode_target, skill_src, config_dir
    ):
        """Fresh Claude/Codex markers must not suppress stale OpenCode repair."""
        skill_link._ensure_retro_skill_links()
        assert skill_link._skill_links_check_due() is False

        opencode_target.unlink()
        assert skill_link._skill_links_check_due() is True

    def test_fresh_marker_with_correct_link_means_not_due(self, target, skill_src, config_dir):
        """Steady state: marker fresh AND link points at our source → skip.
        Both conditions are required post-drift-check."""
        skill_link._ensure_retro_skill_link()
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
# Track 17A: descriptor-driven installation outcomes.
# ---------------------------------------------------------------------------


class TestInstallerResults:
    def test_reports_one_outcome_for_each_agent(
        self, target, codex_target, opencode_target, skill_src
    ):
        results = skill_link._ensure_retro_skill_links()

        assert [result.descriptor.display_name for result in results] == [
            "Claude Code",
            "Codex",
            "OpenCode",
        ]
        assert [result.status for result in results] == ["installed", "installed", "installed"]
        assert [result.target for result in results] == [target, codex_target, opencode_target]

    def test_correct_link_reports_unchanged(self, target, codex_target, opencode_target, skill_src):
        skill_link._ensure_retro_skill_links()
        result = skill_link._ensure_retro_skill_links()[0]

        assert result.status == "unchanged"
        assert result.skill_src == skill_src.resolve()

    def test_conflict_does_not_hide_other_successes(
        self, target, codex_target, opencode_target, skill_src
    ):
        target.write_text("user's own retro-fleet skill")

        results = skill_link._ensure_retro_skill_links()

        assert [result.status for result in results] == ["foreign", "installed", "installed"]
        assert target.read_text() == "user's own retro-fleet skill"
        _assert_store_link(codex_target)
        _assert_store_link(opencode_target)

    def test_dangling_symlink_reports_conflict_without_unlinking(
        self, target, skill_src, _isolate_paths, monkeypatch
    ):
        import pathlib

        gone = _isolate_paths / "old-venv" / "retro_fleet"
        gone.mkdir(parents=True)
        target.symlink_to(gone)
        import shutil

        shutil.rmtree(gone.parent)
        monkeypatch.setattr(
            pathlib.Path,
            "unlink",
            lambda _self, *args, **kwargs: pytest.fail("installer must not unlink dangling links"),
        )

        result = skill_link._ensure_retro_skill_links()[0]

        assert result.status == "foreign"
        assert target.is_symlink()
        assert not target.exists()

    def test_target_resolution_failure_is_not_reported_as_conflict(
        self, target, skill_src, monkeypatch
    ):
        import pathlib

        target.symlink_to(skill_src)
        original = pathlib.Path.resolve

        def fake_resolve(self, *args, **kwargs):
            if self == target:
                raise OSError("simulated EACCES")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "resolve", fake_resolve)

        result = skill_link._ensure_retro_skill_links()[0]

        assert result.status == "failed"
        assert result.reason is not None
        assert "OSError" in result.reason

    def test_source_failure_fans_out_to_every_available_agent(
        self, codex_target, opencode_target, monkeypatch
    ):
        calls = 0

        def boom():
            nonlocal calls
            calls += 1
            raise RuntimeError("source unavailable")

        monkeypatch.setattr(skill_link, "_resolve_retro_skill_src", boom)

        results = skill_link._ensure_retro_skill_links()

        assert calls == 1
        assert [result.status for result in results] == ["failed", "failed", "failed"]
        assert all(result.reason is not None for result in results)

    def test_directory_setup_failure_does_not_hide_other_outcomes(
        self, target, codex_target, skill_src, monkeypatch
    ):
        import pathlib
        import shutil

        shutil.rmtree(codex_target.parent)
        original_mkdir = pathlib.Path.mkdir

        def fake_mkdir(self, *args, **kwargs):
            if self == codex_target.parent:
                raise PermissionError("simulated read-only skills directory")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "mkdir", fake_mkdir)

        results = skill_link._ensure_retro_skill_links()

        assert [result.status for result in results] == ["installed", "failed", "unavailable"]
        _assert_store_link(target)

    def test_dangling_conflict_does_not_hide_other_outcomes(
        self, target, codex_target, skill_src, _isolate_paths
    ):
        import shutil

        gone = _isolate_paths / "old-venv" / "retro_fleet"
        gone.mkdir(parents=True)
        target.symlink_to(gone)
        shutil.rmtree(gone.parent)

        results = skill_link._ensure_retro_skill_links()

        assert [result.status for result in results] == ["foreign", "installed", "unavailable"]
        _assert_store_link(codex_target)

    def test_non_directory_agent_root_is_a_failed_result(self, target, skill_src, _isolate_paths):
        import shutil

        shutil.rmtree(_isolate_paths / ".claude")
        (_isolate_paths / ".claude").write_text("not a directory")

        results = skill_link._ensure_retro_skill_links()

        assert [result.status for result in results] == ["failed", "unavailable", "unavailable"]
        assert results[0].reason is not None
        assert "NotADirectoryError" in results[0].reason

    def test_dry_run_returns_no_results_or_side_effects(self, target, monkeypatch):
        def fail_if_called():
            raise AssertionError("dry-run must not resolve the source")

        monkeypatch.setattr(skill_link, "_resolve_retro_skill_src", fail_if_called)

        results = skill_link._ensure_retro_skill_links(dry_run=True)
        assert results
        assert all(result.status != "installed" for result in results)
        assert not target.exists()
        store = skill_link._skill_store_dir()
        assert not store.exists() or not any(store.iterdir())


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
        _assert_store_link(target)

    def test_idempotent_on_correct_link(self, target, skill_src):
        from mind_meld.cli import app

        target.symlink_to(skill_src)
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert "Installed" in result.output

    def test_reports_dangling_link_as_conflict(self, target, skill_src, _isolate_paths):
        from mind_meld.cli import app

        deleted_target = _isolate_paths / "old-venv" / "skills" / "retro_fleet"
        deleted_target.mkdir(parents=True)
        target.symlink_to(deleted_target)
        import shutil

        shutil.rmtree(deleted_target.parent.parent)

        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 1
        assert "mm: error: Claude Code:" in result.output
        assert target.is_symlink()
        assert not target.exists()

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
        _assert_store_link(codex_target)
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
        _assert_store_link(opencode_target)
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
        _assert_store_link(opencode_target)

    def test_reports_conflict_without_undoing_codex_install(self, target, codex_target, skill_src):
        from mind_meld.cli import app

        target.write_text("user's own retro-fleet")
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 1
        assert target.read_text() == "user's own retro-fleet"
        assert codex_target.is_symlink()
        _assert_store_link(codex_target)
        assert str(codex_target) in result.output
        assert str(target) in result.output

    def test_reports_failed_agent_alongside_success(
        self, target, codex_target, skill_src, monkeypatch
    ):
        from mind_meld.cli import app

        original_symlink = os.symlink

        def fake_symlink(src, dst, target_is_directory=False):
            if Path(dst) == codex_target:
                raise PermissionError("simulated read-only Codex directory")
            return original_symlink(src, dst, target_is_directory)

        monkeypatch.setattr(os, "symlink", fake_symlink)

        result = self._runner().invoke(app, ["install-skills"])

        assert result.exit_code == 1
        assert "Installed: Claude Code:" in result.output
        assert "mm: error: Codex:" in result.output
        assert "PermissionError" in result.output
        _assert_store_link(target)

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
            lambda *, dry_run, allow_mutate=True, explicit=False: calls.append(dry_run),
        )
        monkeypatch.setattr(cli_module, "get_sources", lambda _config: [])

        assert cli_module._push_core(config, "pw", 1024) is None
        assert calls == [False]

    def test_push_keeps_running_when_installer_regresses(self, monkeypatch, capsys):
        config = {
            "device": {"id": "dev-a", "name": "Mac A"},
            "sync": {"max_file_size": 1024},
        }
        monkeypatch.setattr(cli_module, "get_backend", lambda _config: object())
        monkeypatch.setattr(cli_module, "_ensure_device_registered", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(skill_link, "_skill_links_check_due", lambda: True)

        def boom(*, dry_run, allow_mutate=True, explicit=False):
            raise RuntimeError("unexpected installer regression")

        monkeypatch.setattr(skill_link, "_ensure_retro_skill_links", boom)
        monkeypatch.setattr(cli_module, "get_sources", lambda _config: [])

        assert cli_module._push_core(config, "pw", 1024) is None
        assert "retro-fleet skill installation failed" in capsys.readouterr().err


class TestDurableStore:
    def test_publish_failure_leaves_every_link_untouched(
        self, target, codex_target, opencode_target, skill_src, monkeypatch
    ):
        from mind_meld.errors import StorageError

        target.symlink_to(skill_src)
        prior = os.readlink(target)

        def boom(*_args, **_kwargs):
            raise StorageError("disk full")

        monkeypatch.setattr(skill_link, "atomic_write_bytes", boom)
        results = skill_link._ensure_retro_skill_links()
        assert {result.status for result in results} == {"failed"}
        assert target.is_symlink()
        assert os.readlink(target) == prior

    def test_version_bump_republishes_the_store(self, target, skill_src, monkeypatch):
        """The mm-upgrade path -- the entire reason the store exists.

        `_should_publish`'s pkg > store arm had no test: every other arm did.
        Without this, `pipx upgrade mind-meld` could silently leave the three
        agents executing the OLD SKILL.md and nothing would catch it.
        """
        skill_link._ensure_retro_skill_links()
        store = skill_link._skill_store_dir()
        assert (store / "SKILL.md").read_bytes() == (skill_src / "SKILL.md").read_bytes()

        (skill_src / "SKILL.md").write_text("# v99 skill\n")
        monkeypatch.setattr(skill_link, "__version__", "99.0.0")
        skill_link._ensure_retro_skill_links()

        assert (store / "SKILL.md").read_text() == "# v99 skill\n"
        meta = skill_link._read_store_meta(store)
        assert meta["skill_version"] == "99.0.0", meta

    def test_install_skills_run_twice_is_idempotent(self, target, skill_src):
        """Second run must report unchanged, not re-migrate.

        The pre-existing idempotency pin linked the target at `skill_src`, so its
        second run took the MIGRATE path and `"Installed" in output` matched the
        wrong string. Point at the store and the guarantee is real.
        """
        first = skill_link._ensure_retro_skill_links(explicit=True)
        assert first[0].status == "installed"
        store = skill_link._skill_store_dir()
        before = (store / "SKILL.md").stat().st_mtime_ns
        prior_link = os.readlink(target)

        second = skill_link._ensure_retro_skill_links(explicit=True)

        assert second[0].status == "unchanged", second[0]
        assert os.readlink(target) == prior_link
        assert (store / "SKILL.md").stat().st_mtime_ns == before

    def test_explicit_migrates_a_live_checkout_but_push_does_not(self, target, skill_src):
        """The whole behavioral difference between `mm push` and `mm install-skills`.

        Only the leave-alone direction was asserted; nothing proved the explicit
        path actually migrates.
        """
        checkout = target.parent.parent / "co" / "src" / "mind_meld" / "skills" / "retro_fleet"
        checkout.mkdir(parents=True)
        target.symlink_to(checkout)

        push = skill_link._ensure_retro_skill_links(explicit=False)
        assert push[0].status == "unchanged"
        assert push[0].reason == "live-checkout"
        assert os.readlink(target) == str(checkout)

        explicit = skill_link._ensure_retro_skill_links(explicit=True)
        assert explicit[0].status == "installed"
        assert explicit[0].reason == "migrated"
        assert os.readlink(target) == str(skill_link._skill_store_dir())

    def test_autopush_classifies_but_never_mutates(self, target, skill_src, capsys):
        """allow_mutate=False is the mode autopush ships in. It had zero
        deliberate coverage on any path where a link actually exists."""
        foreign = target.parent / "somewhere-else"
        foreign.mkdir()
        target.symlink_to(foreign)
        store = skill_link._skill_store_dir()

        results = skill_link._ensure_retro_skill_links(allow_mutate=False)

        assert os.readlink(target) == str(foreign), "autopush mutated agent config"
        assert not (store / "SKILL.md").exists(), "autopush created the store"
        assert results[0].status == "foreign"

    def test_bad_utf8_in_store_meta_does_not_crash_diagnose(self, target, skill_src):
        """`mm status` and `mm diag` call diagnose_skill_links() with NO enclosing
        try. `_read_store_meta` does read_text(encoding="utf-8"), and
        UnicodeDecodeError is a ValueError -- so one bad byte crashed both of the
        commands you run to diagnose a broken link.
        """
        store = skill_link._skill_store_dir()
        store.mkdir(parents=True, exist_ok=True)
        (store / ".mm-skill.json").write_bytes(b'{"skill_\xff\xfeversion":"1"}')

        assert skill_link._read_store_meta(store) is None
        rows = skill_link.diagnose_skill_links()
        assert len(rows) == 3

    def test_symlink_loop_is_classified_not_crashed_on(self, target, skill_src):
        """A looped link must classify identically on every supported Python.

        3.11/3.12 raise RuntimeError from resolve(); 3.13+ raise OSError(ELOOP).
        Before normalization the same filesystem state was a repairable
        classification on 3.11 and a hard `failed` on the 3.13 CI runs -- and
        the crash escaped diagnose_skill_links entirely.
        """
        target.symlink_to(target)

        assert skill_link._symlink_lives(target) is False
        rows = skill_link.diagnose_skill_links()
        assert len(rows) == 3
        assert rows[0]["status"] != "ok"

    def test_dangling_foreign_link_is_reported_broken(self, target, skill_src):
        """A dangling link to an unrecognized path is BROKEN from the agent's
        view (dead skill entry), even though mm must not touch it. Collapsing it
        into live `foreign` made the mm status allowlist silence a real break.
        """
        target.symlink_to(target.parent / "nowhere" / "gone")

        rows = skill_link.diagnose_skill_links()
        assert rows[0]["status"] == "foreign-dangling"
        assert "foreign-dangling" in skill_link.BROKEN_SKILL_STATUSES

    def test_notice_states_the_real_cause_for_a_dangling_store_link(
        self, target, skill_src, capsys
    ):
        """REGRESSION: the push-path notice said "is not mm's store link" for
        `dangling-ours` -- a link whose readlink byte-equals the store constant,
        which is the very thing that PROVES it is mm's -- and told the user to
        move it aside, the one action that stops mm repairing it.
        """
        store = skill_link._skill_store_dir()
        target.symlink_to(store)

        skill_link._ensure_retro_skill_links(allow_mutate=False)

        err = capsys.readouterr().err
        assert "is not mm's store link" not in err, err
        assert "mm's symlink" in err, err
        assert "store is missing" in err, err
        assert "mm install-skills" in err, err

    def test_classify_only_run_does_not_consume_the_notice_budget(self, target, skill_src, capsys):
        """A run that cannot repair must not spend the 24h conflict marker.

        autopush classifies with allow_mutate=False. If it touched the marker,
        the interactive push that COULD repair the link would be silenced for
        24h -- the notice budget spent by the run that does nothing.
        """
        store = skill_link._skill_store_dir()
        target.symlink_to(store)
        marker_dir = skill_link._marker_dir()

        skill_link._ensure_retro_skill_links(allow_mutate=False)

        markers = [p.name for p in marker_dir.iterdir()] if marker_dir.exists() else []
        assert markers == [], markers

        # ...and the notice still fires on the very next run, not suppressed.
        capsys.readouterr()
        skill_link._ensure_retro_skill_links(allow_mutate=False)
        assert "mm install-skills" in capsys.readouterr().err

    def test_user_authored_skill_md_is_never_overwritten(self, target, skill_src):
        """REGRESSION: `SKILL.md` must not count as proof mm owns the store.

        It is the canonical Agent Skills filename, so a user who hand-authored a
        retro-fleet skill in the store path would have had it silently replaced:
        a payload-only directory read as "owned", the sentinel got planted,
        `_should_publish` saw `meta is None` and published. No backup, no notice.
        """
        store = skill_link._skill_store_dir()
        store.mkdir(parents=True, exist_ok=True)
        mine = store / "SKILL.md"
        mine.write_text("# MY OWN HAND-WRITTEN SKILL\ndo not overwrite me\n")
        before = mine.read_text()

        results = skill_link._ensure_retro_skill_links()

        assert mine.read_text() == before, "mm overwrote a user-authored SKILL.md"
        assert not (store / ".mm-owned").exists(), "mm claimed a foreign store"
        statuses = {r.status for r in results}
        assert "failed" in statuses, statuses
        assert not statuses & {"installed", "unchanged"}, statuses

    def test_store_with_only_mm_metadata_is_still_owned(self, target, skill_src):
        """The mm-namespaced sidecar still proves ownership, so a lost sentinel self-heals."""
        store = skill_link._skill_store_dir()
        store.mkdir(parents=True, exist_ok=True)
        (store / ".mm-skill.json").write_text('{"schema": 1, "skill_version": "0.0.1"}')

        results = skill_link._ensure_retro_skill_links()

        assert (store / "SKILL.md").is_file()
        assert (store / ".mm-owned").exists()
        assert "failed" not in {r.status for r in results}

    def test_store_dir_symlink_to_real_dir_is_refused(self, target, skill_src, _isolate_paths):
        real = _isolate_paths / "elsewhere"
        real.mkdir()
        store = skill_link._skill_store_dir()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.symlink_to(real)
        result = skill_link._ensure_retro_skill_link()
        assert result.status == "failed"
        assert not target.exists() or os.readlink(target) != str(store)

    def test_store_dir_dangling_symlink_is_refused(self, target, skill_src, _isolate_paths):
        store = skill_link._skill_store_dir()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.symlink_to(_isolate_paths / "missing-store")
        result = skill_link._ensure_retro_skill_link()
        assert result.status == "failed"

    def test_store_payload_symlink_is_not_replaced(self, target, skill_src, _isolate_paths):
        store = skill_link._skill_store_dir()
        store.mkdir(parents=True)
        (store / skill_link._STORE_SENTINEL).write_text("mind-meld skill store\n")
        (store / skill_link._STORE_PAYLOAD).symlink_to(_isolate_paths / "other.md")
        result = skill_link._ensure_retro_skill_link()
        assert result.status == "failed"
        assert (store / skill_link._STORE_PAYLOAD).is_symlink()

    def test_store_regular_file_reports_failed_and_preserves_content(
        self, target, skill_src, _isolate_paths
    ):
        store = skill_link._skill_store_dir()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text("not a directory")
        result = skill_link._ensure_retro_skill_link()
        assert result.status == "failed"
        assert store.read_text() == "not a directory"

    def test_foreign_non_empty_store_without_sentinel_is_refused(
        self, target, skill_src, _isolate_paths
    ):
        store = skill_link._skill_store_dir()
        store.mkdir(parents=True)
        (store / "notes.txt").write_text("user data")
        result = skill_link._ensure_retro_skill_link()
        assert result.status == "failed"
        assert (store / "notes.txt").read_text() == "user data"

    def test_migration_never_calls_unlink(self, target, skill_src, monkeypatch):
        target.symlink_to(skill_src)
        original = os.unlink

        def fake_unlink(path, *args, **kwargs):
            if Path(path) == target:
                pytest.fail("installer must not unlink the skill link")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", fake_unlink)
        result = skill_link._ensure_retro_skill_link()
        assert result.status == "installed"
        _assert_store_link(target)

    def test_checkout_shaped_link_is_not_migrated(self, target, skill_src, _isolate_paths, capsys):
        checkout = _isolate_paths / "src" / "mind_meld" / "skills" / "retro_fleet"
        checkout.mkdir(parents=True)
        (checkout / "SKILL.md").write_text("# dogfood")
        target.symlink_to(checkout)
        result = skill_link._ensure_retro_skill_link()
        assert result.status == "unchanged"
        assert result.reason == "live-checkout"
        assert target.resolve() == checkout.resolve()
        assert "leaving it alone" in capsys.readouterr().err

    def test_legacy_dangling_package_link_is_not_classified_foreign(
        self, target, skill_src, _isolate_paths, monkeypatch
    ):
        import shutil

        dead = (
            _isolate_paths
            / "old"
            / "lib"
            / "python3.14"
            / "site-packages"
            / "mind_meld"
            / "skills"
            / "retro_fleet"
        )
        dead.mkdir(parents=True)
        target.symlink_to(dead)
        shutil.rmtree(dead)
        original = os.unlink

        def fake_unlink(path, *args, **kwargs):
            if Path(path) == target:
                pytest.fail("must not unlink dangling legacy link")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", fake_unlink)
        result = skill_link._ensure_retro_skill_link()
        assert result.status == "installed"
        _assert_store_link(target)

    def test_publish_skipped_when_stored_version_is_newer(self, target, skill_src, monkeypatch):
        skill_link._ensure_retro_skill_link()
        store = skill_link._skill_store_dir()
        meta = skill_link._read_store_meta(store)
        meta["skill_version"] = "99.0.0"
        (store / skill_link._STORE_META).write_text(
            __import__("json").dumps(meta), encoding="utf-8"
        )
        before = (store / skill_link._STORE_PAYLOAD).read_bytes()
        (skill_src / "SKILL.md").write_text("# newer package bytes")
        monkeypatch.setattr(skill_link, "__version__", "0.1.0")
        skill_link._ensure_retro_skill_link()
        assert (store / skill_link._STORE_PAYLOAD).read_bytes() == before

    def test_publish_on_equal_version_differing_hash(self, target, skill_src, capsys, monkeypatch):
        skill_link._ensure_retro_skill_link()
        (skill_src / "SKILL.md").write_text("# equal version, new hash")
        monkeypatch.setattr(skill_link, "__version__", skill_link.__version__)
        skill_link._ensure_retro_skill_link()
        assert (skill_link._skill_store_dir() / "SKILL.md").read_text() == (
            "# equal version, new hash"
        )
        assert "republishing" in capsys.readouterr().err

    def test_identical_payload_causes_no_write(self, target, skill_src):
        skill_link._ensure_retro_skill_link()
        store = skill_link._skill_store_dir() / "SKILL.md"
        before = store.stat().st_mtime_ns
        skill_link._ensure_retro_skill_link()
        assert store.stat().st_mtime_ns == before

    def test_version_compare_is_not_lexical(self):
        from packaging.version import Version

        assert Version("0.12.9") < Version("0.12.37")

    def test_store_freshness_trips_the_gate_after_a_version_bump(
        self, target, skill_src, monkeypatch
    ):
        skill_link._ensure_retro_skill_link()
        assert skill_link._skill_link_check_due() is False
        monkeypatch.setattr(skill_link, "__version__", "9.9.9")
        (skill_src / "SKILL.md").write_text("# bumped")
        assert skill_link._skill_link_check_due() is True

    def test_dead_editable_install_reports_unchanged_not_failed(
        self, target, skill_src, monkeypatch
    ):
        skill_link._ensure_retro_skill_link()

        def boom():
            raise ModuleNotFoundError("editable tree gone")

        monkeypatch.setattr(skill_link, "_resolve_retro_skill_src", boom)
        result = skill_link._ensure_retro_skill_link()
        assert result.status == "unchanged"
        _assert_store_link(target)

    def test_store_is_not_inside_any_configured_source(self):
        from mind_meld.config import DEFAULT_SOURCES

        store = Path("~/.local/share/mind-meld/agent-skills/retro-fleet")
        for src in DEFAULT_SOURCES:
            root = Path(src["path"])
            includes = src.get("include_dirs") or []
            for include in includes:
                covered = (root / include).expanduser()
                assert store != covered
                assert covered not in store.parents
                assert store not in covered.parents or include == "."

    def test_store_payload_is_skill_md_only(self, target, skill_src):
        skill_link._ensure_retro_skill_link()
        store = skill_link._skill_store_dir()
        names = {p.name for p in store.iterdir()}
        assert "SKILL.md" in names
        assert "aggregator.py" not in names

    def test_store_file_mode_is_0644_and_dir_is_0700(self, target, skill_src):
        skill_link._ensure_retro_skill_link()
        store = skill_link._skill_store_dir()
        assert oct(store.stat().st_mode & 0o777) == "0o700"
        assert oct((store / "SKILL.md").stat().st_mode & 0o777) == "0o644"

    def test_dry_run_does_not_touch_the_store(self, target, skill_src):
        results = skill_link._ensure_retro_skill_links(dry_run=True)
        assert results
        store = skill_link._skill_store_dir()
        assert not (store / "SKILL.md").exists()
        assert not target.exists()

    def test_installer_refuses_unpatched_real_store(self, target, skill_src, monkeypatch):
        monkeypatch.setattr(
            skill_link,
            "_skill_store_dir",
            lambda: (
                skill_link._REAL_HOME
                / ".local"
                / "share"
                / "mind-meld"
                / "agent-skills"
                / "retro-fleet"
            ),
        )
        result = skill_link._ensure_retro_skill_link()
        assert result is None or result.status == "failed"
