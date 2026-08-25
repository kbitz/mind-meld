"""Group 8 / Track 8A: retro-fleet skill symlink installer pins.

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
* 24h-TTL gate fresh → ``_skill_link_check_due_for`` returns False
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
# _isolate_skill_links redirects roots via the override map, which would
# fight that -- the marker tells it to step aside.
pytestmark = pytest.mark.owns_skill_paths


def _install(key: str = "claude", **kwargs):
    kwargs.setdefault("may_create", frozenset({key}))
    results = skill_link._ensure_retro_skill_links(**kwargs)
    return _result_for(results, key)


def _due(key: str = "claude") -> bool:
    return skill_link._skill_link_check_due_for(skill_link._descriptor_for(key))


def _target_for(home: Path, key: str) -> Path:
    row = next(r for r in skill_link.AGENT_ROWS if r.key == key)
    skills_dir = home / row.skills_root[2:]
    skills_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir / "retro-fleet"


def _result_for(results, key: str = "claude"):
    return next(result for result in results if result.descriptor.key == key)


def _diagnosis_for(rows, key: str = "claude"):
    return next(row for row in rows if row["key"] == key)


def _may(*keys: str) -> frozenset[str]:
    """Consent set without a 2+ key literal (test_no_consumer_owned_agent_name_lists)."""
    return frozenset(keys)


def _write_mm_config(home: Path, *, source_names=("claude",), maintain_links=True, agents=None):
    from mind_meld import config as config_mod
    from mind_meld.config import save_config

    storage = home / "mm-storage"
    storage.mkdir(exist_ok=True)
    sources = []
    for name in source_names:
        if name == "claude":
            path = home / ".claude"
        elif name == "codex":
            path = home / ".codex"
        elif name == "opencode":
            path = home / ".config" / "opencode"
        else:
            path = home / name
        path.mkdir(parents=True, exist_ok=True)
        src_type = name if name in ("claude", "codex", "opencode", "grok") else "generic"
        sources.append({"name": name, "path": str(path), "type": src_type})
    cfg: dict = {
        "device": {"id": "dev-a", "name": "Mac A"},
        "storage": {"path": str(storage)},
        "sync": {"sources": sources, "max_file_size": 1024},
    }
    skills: dict = {}
    if maintain_links is not True:
        skills["maintain_links"] = maintain_links
    if agents is not None:
        skills["agents"] = agents
    if skills:
        cfg["skills"] = skills
    save_config(cfg, config_mod.CONFIG_PATH)
    return cfg


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Point all the installer's filesystem touchpoints at tmp_path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    (fake_home / ".config" / "mind-meld").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # Path("~/...").expanduser() reads HOME, so we just need to set the env.
    # CONFIG_PATH is frozen at import from the real HOME; patch it or
    # install_skills_cmd reads the developer's real config locally and none
    # on CI.
    monkeypatch.setattr(
        "mind_meld.config.CONFIG_PATH",
        fake_home / ".config" / "mind-meld" / "config.toml",
    )
    yield fake_home


@pytest.fixture
def target(_isolate_paths):
    return _target_for(_isolate_paths, "claude")


@pytest.fixture
def agent_targets(_isolate_paths):
    """Every registry row's retro-fleet target, with its skills dir created."""
    return {row.key: _target_for(_isolate_paths, row.key) for row in skill_link.AGENT_ROWS}


@pytest.fixture
def codex_target(_isolate_paths):
    return _target_for(_isolate_paths, "codex")


@pytest.fixture
def opencode_target(_isolate_paths):
    return _target_for(_isolate_paths, "opencode")


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
        _install()
        _assert_store_link(target)
        # Success marker touched.
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        assert marker.exists()

    def test_creates_codex_symlink_when_target_absent(self, codex_target, skill_src, config_dir):
        _install("codex")
        _assert_store_link(codex_target)
        marker = config_dir / f".{skill_link._CODEX_SKILL_LINK_SUCCESS_MARKER}"
        assert marker.exists()

    def test_creates_opencode_symlink_when_target_absent(
        self, opencode_target, skill_src, config_dir
    ):
        _install("opencode")
        _assert_store_link(opencode_target)
        marker = config_dir / f".{skill_link._OPENCODE_SKILL_LINK_SUCCESS_MARKER}"
        assert marker.exists()

    @pytest.mark.parametrize("key", [row.key for row in skill_link.AGENT_ROWS])
    def test_creates_missing_skills_directory_when_agent_is_installed(
        self, _isolate_paths, skill_src, key
    ):
        import shutil

        descriptor = skill_link._descriptor_for(key)
        skills_dir = descriptor.skills_dir
        if skills_dir.exists():
            shutil.rmtree(skills_dir)
        skills_dir.parent.mkdir(parents=True, exist_ok=True)

        _install(key)

        assert descriptor.target.is_symlink()
        _assert_store_link(descriptor.target)


# ---------------------------------------------------------------------------
# Branch 2: target is correct symlink → no-op.
# ---------------------------------------------------------------------------


class TestTargetCorrect:
    def test_correct_symlink_is_noop(self, target, skill_src, config_dir):
        _install()
        _install()
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
        _install()

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
        _install()
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
        assert "restart the agent so it reloads SKILL.md" in captured.err, captured.err

    def test_wrong_symlink_target_not_clobbered(self, target, skill_src, _isolate_paths, capsys):
        # User pointed retro-fleet at their own skill dir.
        their_dir = _isolate_paths / "their-skill"
        their_dir.mkdir()
        target.symlink_to(their_dir)
        _install()
        # Symlink unchanged.
        assert target.resolve() == their_dir.resolve()
        captured = capsys.readouterr()
        assert "mm: notice:" in captured.err

    def test_conflict_notice_suppressed_within_24h(self, target, skill_src, config_dir, capsys):
        """cross-model #3: per-push spam on conflict is hostile. Two-marker
        gate suppresses the notice within 24h."""
        target.write_text("user's file")
        # First call — emits notice + touches conflict marker.
        _install()
        first = capsys.readouterr()
        assert "mm: notice:" in first.err
        # Second call within the TTL → marker is fresh, no notice.
        _install()
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
        _install()
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
        _install()  # Must not raise.
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
        assert _due() is True

    @pytest.mark.parametrize("key", [row.key for row in skill_link.AGENT_ROWS])
    def test_present_agent_root_without_skills_directory_is_due(self, config_dir, key):
        """A fresh legacy marker must not delay repair of a missing skills dir."""
        import shutil

        descriptor = skill_link._descriptor_for(key)
        skills_dir = descriptor.skills_dir
        if skills_dir.exists():
            shutil.rmtree(skills_dir)
        skills_dir.parent.mkdir(parents=True, exist_ok=True)
        (config_dir / f".{descriptor.success_marker}").touch()

        assert _due(key) is True

    def test_codex_fresh_marker_with_correct_link_means_not_due(
        self, codex_target, skill_src, config_dir
    ):
        _install("codex")
        assert _due("codex") is False

    def test_combined_gate_repairs_codex_when_claude_is_healthy(
        self, target, codex_target, skill_src, config_dir
    ):
        """A fresh Claude marker must not suppress an independently stale Codex link."""
        skill_link._ensure_retro_skill_links(may_create=None)
        assert skill_link._skill_links_check_due(may_create=None) is False

        # Damage, not deletion: an absent target is now "the user removed it"
        # (Track 28A) and is deliberately NOT due. Repointing keeps this test on
        # its actual subject -- per-row independence of the gate.
        codex_target.unlink()
        codex_target.symlink_to(codex_target.parent / "not-the-store")
        assert skill_link._skill_links_check_due(may_create=None) is True

    def test_combined_gate_repairs_opencode_when_other_agents_are_healthy(
        self, target, codex_target, opencode_target, skill_src, config_dir
    ):
        """Fresh Claude/Codex markers must not suppress stale OpenCode repair."""
        skill_link._ensure_retro_skill_links(may_create=None)
        assert skill_link._skill_links_check_due(may_create=None) is False

        opencode_target.unlink()
        opencode_target.symlink_to(opencode_target.parent / "not-the-store")
        assert skill_link._skill_links_check_due(may_create=None) is True

    def test_fresh_marker_with_correct_link_means_not_due(self, target, skill_src, config_dir):
        """Steady state: marker fresh AND link points at our source → skip.
        Both conditions are required post-drift-check."""
        _install()
        assert _due() is False

    def test_stale_marker_means_due(self, target, skill_src, config_dir):
        target.symlink_to(skill_src)
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        old = time.time() - (25 * 3600)
        os.utime(marker, (old, old))
        assert _due() is True

    def test_fresh_marker_but_link_missing_is_not_due(self, skill_src, config_dir):
        """INVERTED by Track 28A, deliberately.

        This used to pin auto-recovery: marker touched by an earlier push, link
        later removed by hand, so the drift check tripped and push rebuilt it.
        That is now the definition of a user removal, and push leaves it alone.
        The recovery path did not disappear -- it became explicit, and
        ``install_skills_cmd``'s docstring already names this exact case as its
        first use ("post-cleanup recovery (link removed by hand...)").
        """
        marker = config_dir / f".{skill_link._SKILL_LINK_SUCCESS_MARKER}"
        marker.touch()
        assert _due() is False

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
        assert _due() is True

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
        assert _due() is True

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
        assert _due() is True

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
        assert _due() is True


# ---------------------------------------------------------------------------
# dry_run preview contract.
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_is_noop(self, target, skill_src, config_dir):
        """`mm push --dry-run` must NOT mutate the symlink or the marker."""
        _install(dry_run=True)
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

    def test_skill_targets_are_exactly_the_installer_targets(self, agent_targets):
        """Order matches ``AGENT_ROWS``, matching this file's row-keyed helper."""
        assert skill_link.skill_targets() == tuple(
            agent_targets[row.key] for row in skill_link.AGENT_ROWS
        )

    def test_each_wrapper_installs_at_its_skill_targets_entry(self, agent_targets, skill_src):
        """The stronger form: drive the real installers and confirm the links
        land exactly where ``skill_targets()`` says they will.

        Comparing tuples alone would still pass if BOTH sides drifted together
        (e.g. someone renames a row's ``skills_root``);
        asserting on the on-disk result is what makes that visible.
        """
        skill_link._ensure_retro_skill_links(may_create=None)
        expected = [agent_targets[row.key] for row in skill_link.AGENT_ROWS]
        assert [t for t in skill_link.skill_targets() if t.is_symlink()] == expected

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
    def test_reports_one_outcome_for_each_agent(self, agent_targets, skill_src):
        results = skill_link._ensure_retro_skill_links(may_create=None)

        assert [result.descriptor.display_name for result in results] == [
            row.display_name for row in skill_link.AGENT_ROWS
        ]
        assert [result.status for result in results] == ["installed"] * len(skill_link.AGENT_ROWS)
        assert [result.target for result in results] == [
            agent_targets[row.key] for row in skill_link.AGENT_ROWS
        ]

    def test_correct_link_reports_unchanged(self, agent_targets, skill_src):
        skill_link._ensure_retro_skill_links(may_create=None)
        result = _result_for(skill_link._ensure_retro_skill_links(may_create=None))

        assert result.status == "unchanged"
        assert result.skill_src == skill_src.resolve()

    def test_conflict_does_not_hide_other_successes(self, agent_targets, skill_src):
        agent_targets["claude"].write_text("user's own retro-fleet skill")

        results = skill_link._ensure_retro_skill_links(may_create=None)
        by_key = {result.descriptor.key: result for result in results}

        assert by_key["claude"].status == "foreign"
        assert agent_targets["claude"].read_text() == "user's own retro-fleet skill"
        for row in skill_link.AGENT_ROWS:
            if row.key == "claude":
                continue
            assert by_key[row.key].status == "installed"
            _assert_store_link(agent_targets[row.key])

    def test_declined_row_keeps_results_complete_and_ordered(self, agent_targets, skill_src):
        results = skill_link._ensure_retro_skill_links(may_create=frozenset({"claude"}))
        assert [result.descriptor.key for result in results] == [
            row.key for row in skill_link.AGENT_ROWS
        ]
        by_key = {result.descriptor.key: result for result in results}
        assert by_key["claude"].status == "installed"
        assert by_key["codex"].status == "declined"
        assert by_key["opencode"].status == "declined"

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

        result = _result_for(skill_link._ensure_retro_skill_links(may_create=None))

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

        result = _result_for(skill_link._ensure_retro_skill_links(may_create=None))

        assert result.status == "failed"
        assert result.reason is not None
        assert "OSError" in result.reason

    def test_source_failure_fans_out_to_every_available_agent(self, agent_targets, monkeypatch):
        calls = 0

        def boom():
            nonlocal calls
            calls += 1
            raise RuntimeError("source unavailable")

        monkeypatch.setattr(skill_link, "_resolve_retro_skill_src", boom)

        results = skill_link._ensure_retro_skill_links(may_create=None)

        assert calls == 1
        assert [result.status for result in results] == ["failed"] * len(skill_link.AGENT_ROWS)
        assert all(result.reason is not None for result in results)

    def test_directory_setup_failure_does_not_hide_other_outcomes(
        self, target, skill_src, _isolate_paths, monkeypatch
    ):
        import pathlib

        others = [row for row in skill_link.AGENT_ROWS if row.key != "claude"]
        fail_row, *rest = others
        fail_skills = Path(fail_row.skills_root).expanduser()
        fail_skills.parent.mkdir(parents=True, exist_ok=True)
        original_mkdir = pathlib.Path.mkdir

        def fake_mkdir(self, *args, **kwargs):
            if self == fail_skills:
                raise PermissionError("simulated read-only skills directory")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "mkdir", fake_mkdir)

        results = skill_link._ensure_retro_skill_links(may_create=None)
        by_key = {result.descriptor.key: result.status for result in results}

        assert by_key["claude"] == "installed"
        assert by_key[fail_row.key] == "failed"
        for row in rest:
            assert by_key[row.key] == "unavailable"
        _assert_store_link(target)

    def test_dangling_conflict_does_not_hide_other_outcomes(
        self, target, skill_src, _isolate_paths
    ):
        import shutil

        others = [row for row in skill_link.AGENT_ROWS if row.key != "claude"]
        keep_row, *rest = others
        keep_target = _target_for(_isolate_paths, keep_row.key)

        gone = _isolate_paths / "old-venv" / "retro_fleet"
        gone.mkdir(parents=True)
        target.symlink_to(gone)
        shutil.rmtree(gone.parent)

        results = skill_link._ensure_retro_skill_links(may_create=None)
        by_key = {result.descriptor.key: result.status for result in results}

        assert by_key["claude"] == "foreign"
        assert by_key[keep_row.key] == "installed"
        for row in rest:
            assert by_key[row.key] == "unavailable"
        _assert_store_link(keep_target)

    def test_non_directory_agent_root_is_a_failed_result(self, target, skill_src, _isolate_paths):
        import shutil

        shutil.rmtree(_isolate_paths / ".claude")
        (_isolate_paths / ".claude").write_text("not a directory")

        results = skill_link._ensure_retro_skill_links(may_create=None)
        by_key = {result.descriptor.key: result for result in results}

        assert by_key["claude"].status == "failed"
        assert by_key["claude"].reason is not None
        assert "NotADirectoryError" in by_key["claude"].reason
        for row in skill_link.AGENT_ROWS:
            if row.key != "claude":
                assert by_key[row.key].status == "unavailable"

    def test_dry_run_returns_no_results_or_side_effects(self, target, monkeypatch):
        def fail_if_called():
            raise AssertionError("dry-run must not resolve the source")

        monkeypatch.setattr(skill_link, "_resolve_retro_skill_src", fail_if_called)

        results = skill_link._ensure_retro_skill_links(dry_run=True, may_create=None)
        assert results
        assert all(result.status != "installed" for result in results)
        assert not target.exists()
        store = skill_link._skill_store_dir()
        assert not store.exists() or not any(store.iterdir())


# ---------------------------------------------------------------------------
# `mm install-skills` user-facing command.
# ---------------------------------------------------------------------------


class TestInstallSkillsCommand:
    """The user-facing companion to ``_ensure_retro_skill_links``: explicit
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
        assert "no supported agent skills directory exists" in result.output

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
        monkeypatch.setattr(skill_link, "_skill_links_check_due", lambda *, may_create: True)
        monkeypatch.setattr(
            skill_link,
            "_ensure_retro_skill_links",
            lambda *, dry_run, allow_mutate=True, explicit=False, may_create: calls.append(dry_run),
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
        monkeypatch.setattr(skill_link, "_skill_links_check_due", lambda *, may_create: True)

        def boom(*, dry_run, allow_mutate=True, explicit=False, may_create):
            raise RuntimeError("unexpected installer regression")

        monkeypatch.setattr(skill_link, "_ensure_retro_skill_links", boom)
        monkeypatch.setattr(cli_module, "get_sources", lambda _config: [])

        assert cli_module._push_core(config, "pw", 1024) is None
        assert "retro-fleet skill installation failed" in capsys.readouterr().err

    def test_push_gate_and_installer_share_may_create_and_call_get_sources_once(self, monkeypatch):
        gate_args: list = []
        install_args: list = []
        gs_calls: list = []

        def gs(_config):
            gs_calls.append(1)
            return []

        def gate(*, may_create):
            gate_args.append(may_create)
            return True

        def installer(*, dry_run=False, allow_mutate=True, explicit=False, may_create):
            install_args.append(may_create)
            return ()

        config = {
            "device": {"id": "dev-a", "name": "Mac A"},
            "sync": {"max_file_size": 1024},
            "skills": {"agents": [skill_link.AGENT_ROWS[0].key]},
        }
        monkeypatch.setattr(cli_module, "get_backend", lambda _config: object())
        monkeypatch.setattr(cli_module, "_ensure_device_registered", lambda *_a, **_k: None)
        monkeypatch.setattr(skill_link, "_skill_links_check_due", gate)
        monkeypatch.setattr(skill_link, "_ensure_retro_skill_links", installer)
        monkeypatch.setattr(cli_module, "get_sources", gs)

        assert cli_module._push_core(config, "pw", 1024) is None
        assert gs_calls == [1]
        assert gate_args == install_args
        assert gate_args == [frozenset({skill_link.AGENT_ROWS[0].key})]


class TestDurableStore:
    def test_publish_failure_leaves_every_link_untouched(
        self, agent_targets, skill_src, monkeypatch
    ):
        from mind_meld.errors import StorageError

        claude = agent_targets["claude"]
        claude.symlink_to(skill_src)
        prior = os.readlink(claude)

        def boom(*_args, **_kwargs):
            raise StorageError("disk full")

        monkeypatch.setattr(skill_link, "atomic_write_bytes", boom)
        results = skill_link._ensure_retro_skill_links(may_create=None)
        assert {result.status for result in results} == {"failed"}
        assert claude.is_symlink()
        assert os.readlink(claude) == prior

    def test_version_bump_republishes_the_store(self, target, skill_src, monkeypatch):
        """The mm-upgrade path -- the entire reason the store exists.

        `_should_publish`'s pkg > store arm had no test: every other arm did.
        Without this, `pipx upgrade mind-meld` could silently leave every
        agent executing the OLD SKILL.md and nothing would catch it.
        """
        skill_link._ensure_retro_skill_links(may_create=None)
        store = skill_link._skill_store_dir()
        assert (store / "SKILL.md").read_bytes() == (skill_src / "SKILL.md").read_bytes()

        (skill_src / "SKILL.md").write_text("# v99 skill\n")
        monkeypatch.setattr(skill_link, "__version__", "99.0.0")
        skill_link._ensure_retro_skill_links(may_create=None)

        assert (store / "SKILL.md").read_text() == "# v99 skill\n"
        meta = skill_link._read_store_meta(store)
        assert meta["skill_version"] == "99.0.0", meta

    def test_install_skills_run_twice_is_idempotent(self, target, skill_src):
        """Second run must report unchanged, not re-migrate.

        The pre-existing idempotency pin linked the target at `skill_src`, so its
        second run took the MIGRATE path and `"Installed" in output` matched the
        wrong string. Point at the store and the guarantee is real.
        """
        first = skill_link._ensure_retro_skill_links(explicit=True, may_create=None)
        assert _result_for(first).status == "installed"
        store = skill_link._skill_store_dir()
        before = (store / "SKILL.md").stat().st_mtime_ns
        prior_link = os.readlink(target)

        second = skill_link._ensure_retro_skill_links(explicit=True, may_create=None)

        assert _result_for(second).status == "unchanged", _result_for(second)
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

        push = skill_link._ensure_retro_skill_links(explicit=False, may_create=None)
        assert _result_for(push).status == "unchanged"
        assert _result_for(push).reason == "live-checkout"
        assert os.readlink(target) == str(checkout)

        explicit = skill_link._ensure_retro_skill_links(explicit=True, may_create=None)
        assert _result_for(explicit).status == "installed"
        assert _result_for(explicit).reason == "migrated"
        assert os.readlink(target) == str(skill_link._skill_store_dir())

    def test_autopush_classifies_but_never_mutates(self, target, skill_src, capsys):
        """allow_mutate=False is the mode autopush ships in. It had zero
        deliberate coverage on any path where a link actually exists."""
        foreign = target.parent / "somewhere-else"
        foreign.mkdir()
        target.symlink_to(foreign)
        store = skill_link._skill_store_dir()

        results = skill_link._ensure_retro_skill_links(allow_mutate=False, may_create=None)

        assert os.readlink(target) == str(foreign), "autopush mutated agent config"
        assert not (store / "SKILL.md").exists(), "autopush created the store"
        assert _result_for(results).status == "foreign"

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
        assert len(rows) == len(skill_link.AGENT_ROWS)

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
        assert len(rows) == len(skill_link.AGENT_ROWS)
        assert _diagnosis_for(rows)["status"] != "ok"

    def test_dangling_foreign_link_is_reported_broken(self, target, skill_src):
        """A dangling link to an unrecognized path is BROKEN from the agent's
        view (dead skill entry), even though mm must not touch it. Collapsing it
        into live `foreign` made the mm status allowlist silence a real break.
        """
        target.symlink_to(target.parent / "nowhere" / "gone")

        rows = skill_link.diagnose_skill_links()
        assert _diagnosis_for(rows)["status"] == "foreign-dangling"
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

        skill_link._ensure_retro_skill_links(allow_mutate=False, may_create=None)

        err = capsys.readouterr().err
        assert "is not mm's store link" not in err, err
        assert "mm's symlink" in err, err
        assert "store is missing" in err, err
        assert "mm install-skills" in err, err
        assert "restart the agent so it reloads SKILL.md" in err, err

    def test_classify_only_run_does_not_consume_the_notice_budget(self, target, skill_src, capsys):
        """A run that cannot repair must not spend the 24h conflict marker.

        autopush classifies with allow_mutate=False. If it touched the marker,
        the interactive push that COULD repair the link would be silenced for
        24h -- the notice budget spent by the run that does nothing.
        """
        store = skill_link._skill_store_dir()
        target.symlink_to(store)
        marker_dir = skill_link._marker_dir()

        skill_link._ensure_retro_skill_links(allow_mutate=False, may_create=None)

        markers = [p.name for p in marker_dir.iterdir()] if marker_dir.exists() else []
        assert markers == [], markers

        # ...and the notice still fires on the very next run, not suppressed.
        capsys.readouterr()
        skill_link._ensure_retro_skill_links(allow_mutate=False, may_create=None)
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

        results = skill_link._ensure_retro_skill_links(may_create=None)

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

        results = skill_link._ensure_retro_skill_links(may_create=None)

        assert (store / "SKILL.md").is_file()
        assert (store / ".mm-owned").exists()
        assert "failed" not in {r.status for r in results}

    def test_store_dir_symlink_to_real_dir_is_refused(self, target, skill_src, _isolate_paths):
        real = _isolate_paths / "elsewhere"
        real.mkdir()
        store = skill_link._skill_store_dir()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.symlink_to(real)
        result = _install()
        assert result.status == "failed"
        assert not target.exists() or os.readlink(target) != str(store)

    def test_store_dir_dangling_symlink_is_refused(self, target, skill_src, _isolate_paths):
        store = skill_link._skill_store_dir()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.symlink_to(_isolate_paths / "missing-store")
        result = _install()
        assert result.status == "failed"

    def test_store_payload_symlink_is_not_replaced(self, target, skill_src, _isolate_paths):
        store = skill_link._skill_store_dir()
        store.mkdir(parents=True)
        (store / skill_link._STORE_SENTINEL).write_text("mind-meld skill store\n")
        (store / skill_link._STORE_PAYLOAD).symlink_to(_isolate_paths / "other.md")
        result = _install()
        assert result.status == "failed"
        assert (store / skill_link._STORE_PAYLOAD).is_symlink()

    def test_store_regular_file_reports_failed_and_preserves_content(
        self, target, skill_src, _isolate_paths
    ):
        store = skill_link._skill_store_dir()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text("not a directory")
        result = _install()
        assert result.status == "failed"
        assert store.read_text() == "not a directory"

    def test_foreign_non_empty_store_without_sentinel_is_refused(
        self, target, skill_src, _isolate_paths
    ):
        store = skill_link._skill_store_dir()
        store.mkdir(parents=True)
        (store / "notes.txt").write_text("user data")
        result = _install()
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
        result = _install()
        assert result.status == "installed"
        _assert_store_link(target)

    def test_checkout_shaped_link_is_not_migrated(self, target, skill_src, _isolate_paths, capsys):
        checkout = _isolate_paths / "src" / "mind_meld" / "skills" / "retro_fleet"
        checkout.mkdir(parents=True)
        (checkout / "SKILL.md").write_text("# dogfood")
        target.symlink_to(checkout)
        result = _install()
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
        result = _install()
        assert result.status == "installed"
        _assert_store_link(target)

    def test_publish_skipped_when_stored_version_is_newer(self, target, skill_src, monkeypatch):
        _install()
        store = skill_link._skill_store_dir()
        meta = skill_link._read_store_meta(store)
        meta["skill_version"] = "99.0.0"
        (store / skill_link._STORE_META).write_text(
            __import__("json").dumps(meta), encoding="utf-8"
        )
        before = (store / skill_link._STORE_PAYLOAD).read_bytes()
        (skill_src / "SKILL.md").write_text("# newer package bytes")
        monkeypatch.setattr(skill_link, "__version__", "0.1.0")
        _install()
        assert (store / skill_link._STORE_PAYLOAD).read_bytes() == before

    def test_publish_on_equal_version_differing_hash(self, target, skill_src, capsys, monkeypatch):
        _install()
        (skill_src / "SKILL.md").write_text("# equal version, new hash")
        monkeypatch.setattr(skill_link, "__version__", skill_link.__version__)
        _install()
        assert (skill_link._skill_store_dir() / "SKILL.md").read_text() == (
            "# equal version, new hash"
        )
        assert "republishing" in capsys.readouterr().err

    def test_identical_payload_causes_no_write(self, target, skill_src):
        _install()
        store = skill_link._skill_store_dir() / "SKILL.md"
        before = store.stat().st_mtime_ns
        _install()
        assert store.stat().st_mtime_ns == before

    def test_version_compare_is_not_lexical(self):
        from packaging.version import Version

        assert Version("0.12.9") < Version("0.12.37")

    def test_store_freshness_trips_the_gate_after_a_version_bump(
        self, target, skill_src, monkeypatch
    ):
        _install()
        assert _due() is False
        monkeypatch.setattr(skill_link, "__version__", "9.9.9")
        (skill_src / "SKILL.md").write_text("# bumped")
        assert _due() is True

    def test_dead_editable_install_reports_unchanged_not_failed(
        self, target, skill_src, monkeypatch
    ):
        _install()

        def boom():
            raise ModuleNotFoundError("editable tree gone")

        monkeypatch.setattr(skill_link, "_resolve_retro_skill_src", boom)
        result = _install()
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
        _install()
        store = skill_link._skill_store_dir()
        names = {p.name for p in store.iterdir()}
        assert "SKILL.md" in names
        assert "aggregator.py" not in names

    def test_store_file_mode_is_0644_and_dir_is_0700(self, target, skill_src):
        _install()
        store = skill_link._skill_store_dir()
        assert oct(store.stat().st_mode & 0o777) == "0o700"
        assert oct((store / "SKILL.md").stat().st_mode & 0o777) == "0o644"

    def test_dry_run_does_not_touch_the_store(self, target, skill_src):
        results = skill_link._ensure_retro_skill_links(dry_run=True, may_create=None)
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
        result = _install()
        assert result.status in {"failed", "unavailable"}
        assert result.status != "installed"


class TestConsentDerivation:
    def test_agents_absent_derives_from_passed_in_sources(self):
        keys = skill_link.consented_agent_keys(
            {"skills": {}}, [{"name": "claude"}, {"name": "gstack"}]
        )
        assert keys == frozenset({"claude"})

    def test_agents_present_wins_over_derivation(self):
        keys = skill_link.consented_agent_keys(
            {"skills": {"agents": ["codex"]}},
            [{"name": "claude"}, {"name": "codex"}],
        )
        assert keys == frozenset({"codex"})

    def test_maintain_links_false_ignores_agents(self):
        keys = skill_link.consented_agent_keys(
            {"skills": {"maintain_links": False, "agents": ["claude"]}},
            [{"name": "claude"}],
        )
        assert keys == frozenset()

    def test_config_none_returns_all_row_keys(self):
        assert skill_link.consented_agent_keys(None, []) == frozenset(
            row.key for row in skill_link.AGENT_ROWS
        )

    def test_unknown_agent_name_is_inert(self):
        keys = skill_link.consented_agent_keys({"skills": {"agents": ["codex", "nope"]}}, [])
        assert keys == frozenset({"codex"})

    def test_row_is_consented_none_allows_all(self):
        assert skill_link._row_is_consented("claude", None) is True
        assert skill_link._row_is_consented("codex", None) is True

    def test_writers_require_may_create(self):
        with pytest.raises(TypeError):
            skill_link._ensure_retro_skill_links()
        with pytest.raises(TypeError):
            skill_link._skill_links_check_due()


class TestConsentGate:
    def test_declined_row_never_stats_agent_root(self, agent_targets, skill_src, monkeypatch):
        declined_root = os.path.abspath(skill_link._descriptor_for("codex").agent_root)
        original = Path.stat

        def fake_stat(self, *args, **kwargs):
            if os.path.abspath(self) == declined_root:
                raise AssertionError("declined row must not stat agent root")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fake_stat)
        results = skill_link._ensure_retro_skill_links(may_create=frozenset({"claude"}))
        assert _result_for(results, "codex").status == "declined"
        assert _result_for(results, "claude").status == "installed"

    def test_declined_row_touches_no_marker_and_emits_no_failure(
        self, agent_targets, skill_src, config_dir, capsys
    ):
        marker = config_dir / f".{skill_link._CODEX_SKILL_LINK_SUCCESS_MARKER}"
        results = skill_link._ensure_retro_skill_links(may_create=frozenset({"claude"}))
        assert _result_for(results, "codex").status == "declined"
        assert not marker.exists()
        err = capsys.readouterr().err
        assert "Codex retro-fleet" not in err
        assert "failed:" not in err or "Codex" not in err

    def test_declined_row_never_opens_ttl_gate(self, agent_targets, skill_src, config_dir):
        skill_link._ensure_retro_skill_links(may_create=frozenset({"claude"}))
        assert skill_link._skill_links_check_due(may_create=frozenset({"claude"})) is False

    def test_dry_run_and_quiet_declined_still_declined(self, agent_targets, skill_src):
        dry = skill_link._ensure_retro_skill_links(dry_run=True, may_create=frozenset({"claude"}))
        quiet = skill_link._ensure_retro_skill_links(
            allow_mutate=False, may_create=frozenset({"claude"})
        )
        assert _result_for(dry, "codex").status == "declined"
        assert _result_for(quiet, "codex").status == "declined"
        assert not agent_targets["codex"].exists()

    def test_declined_not_in_broken_skill_statuses(self):
        assert "declined" not in skill_link.BROKEN_SKILL_STATUSES

    def test_render_declined_is_not_the_generic_fallback(self, agent_targets):
        result = skill_link._declined_result(skill_link._descriptor_for("codex"))
        text = skill_link.render_skill_status(result)
        assert ": declined" not in text
        assert "mm install-skills --agent codex" in text
        assert "codex" in text
        assert "sync source" not in text

    def test_render_declined_names_surviving_link(self, agent_targets, skill_src):
        skill_link._ensure_retro_skill_links(may_create=None)
        result = skill_link._declined_result(skill_link._descriptor_for("codex"))
        text = skill_link.render_skill_status(result)
        assert "still present" in text
        assert str(agent_targets["codex"]) in text

    def test_render_declined_dangling_link_does_not_claim_it_works(self, agent_targets, skill_src):
        skill_link._ensure_retro_skill_links(may_create=None)
        store = skill_link._skill_store_dir()
        store.rename(store.with_name("missing-store"))

        result = skill_link._declined_result(skill_link._descriptor_for("codex"))
        text = skill_link.render_skill_status(result)
        assert "still works" not in text


class TestStoreRefreshCarveOut:
    def test_owned_stale_store_refreshes_when_every_row_declined(self, agent_targets, skill_src):
        import json

        skill_link._ensure_retro_skill_links(may_create=None)
        store = skill_link._skill_store_dir()
        payload = store / "SKILL.md"
        original = payload.read_bytes()
        data = json.loads((store / ".mm-skill.json").read_text())
        data["skill_version"] = "0.0.1"
        (store / ".mm-skill.json").write_text(json.dumps(data))
        payload.write_bytes(original + b"\n# stale\n")

        assert skill_link._owned_store_exists()
        assert skill_link._store_needs_refresh()
        assert skill_link._skill_links_check_due(may_create=frozenset()) is True

        results = skill_link._ensure_retro_skill_links(may_create=frozenset())
        assert all(r.status == "declined" for r in results)
        assert payload.read_bytes() == original
        for target in agent_targets.values():
            assert target.is_symlink()

    def test_absent_store_is_not_created_when_every_row_declined(self, agent_targets):
        store = skill_link._skill_store_dir()
        assert not skill_link._owned_store_exists()
        assert skill_link._skill_links_check_due(may_create=frozenset()) is False
        results = skill_link._ensure_retro_skill_links(may_create=frozenset())
        assert all(r.status == "declined" for r in results)
        assert not skill_link._owned_store_exists()
        assert not store.exists() or not any(store.iterdir())
        for target in agent_targets.values():
            assert not target.exists()

    def test_foreign_store_is_left_alone_when_every_row_declined(self, agent_targets):
        store = skill_link._skill_store_dir()
        store.mkdir(parents=True)
        payload = store / "SKILL.md"
        payload.write_text("user's own skill\n")
        assert not skill_link._owned_store_exists()
        assert skill_link._skill_links_check_due(may_create=frozenset()) is False
        results = skill_link._ensure_retro_skill_links(may_create=frozenset())
        assert all(r.status == "declined" for r in results)
        assert payload.read_text() == "user's own skill\n"
        assert not (store / ".mm-owned").exists()


class TestDiagnosePolicyField:
    def test_ok_status_and_disabled_policy_coexist(self, agent_targets, skill_src):
        skill_link._ensure_retro_skill_links(may_create=None)
        rows = skill_link.diagnose_skill_links(may_create=frozenset({"claude"}))
        claude = _diagnosis_for(rows, "claude")
        codex = _diagnosis_for(rows, "codex")
        assert claude["status"] == "ok"
        assert claude["maintain_links"] == "enabled"
        assert codex["status"] == "ok"
        assert codex["maintain_links"] == "disabled (not authorized by skill-link policy)"

    def test_bare_diagnose_does_not_assert_unresolved_policy(self, agent_targets):
        rows = skill_link.diagnose_skill_links()
        assert all(row["maintain_links"] == "unknown (policy not resolved)" for row in rows)

    def test_invalid_config_renders_unknown_not_disabled(self, agent_targets):
        rows = skill_link.diagnose_skill_links(
            may_create=frozenset(), config_error="failed to parse"
        )
        for row in rows:
            assert row["maintain_links"].startswith("unknown (config invalid:")
            assert "disabled" not in row["maintain_links"]

    def test_explicit_decline_names_the_skill_link_policy(self, agent_targets):
        rows = skill_link.diagnose_skill_links(may_create=frozenset({"claude"}))
        codex = _diagnosis_for(rows, "codex")
        assert codex["maintain_links"] == "disabled (not authorized by skill-link policy)"


class TestConsentFlipFlop:
    def test_consent_churn_does_not_resurrect_a_deleted_link(
        self, agent_targets, skill_src, config_dir
    ):
        """Track 28A: the two switches are independent.

        Consent answers "may mm maintain this row". Presence answers "is there
        a link". Re-granting consent does not un-delete -- otherwise flipping a
        source off and on would silently undo a deliberate removal. The undo is
        ``mm install-skills``, which is explicit and skips the guard.
        """
        first = skill_link._ensure_retro_skill_links(may_create=_may("claude", "codex"))
        assert _result_for(first, "codex").status == "installed"
        _assert_store_link(agent_targets["codex"])

        agent_targets["codex"].unlink()
        second = skill_link._ensure_retro_skill_links(may_create=frozenset({"claude"}))
        assert _result_for(second, "codex").status == "declined"
        assert not agent_targets["codex"].exists()

        # Consent comes back. The gate stays shut and push does not rebuild.
        assert skill_link._skill_links_check_due(may_create=_may("claude", "codex")) is False
        third = skill_link._ensure_retro_skill_links(may_create=_may("claude", "codex"))
        assert _result_for(third, "codex").status == "removed-by-user"
        assert not agent_targets["codex"].exists()

        # Explicit install is the documented recovery, and it still works.
        fourth = skill_link._ensure_retro_skill_links(
            explicit=True, may_create=_may("claude", "codex")
        )
        assert _result_for(fourth, "codex").status == "installed"
        _assert_store_link(agent_targets["codex"])

    def test_declined_row_link_still_repaired_when_consent_returns(
        self, agent_targets, skill_src, config_dir
    ):
        """The TTL half of the old test, kept on its own subject.

        A row that was declined and whose link is DAMAGED (present but wrong)
        must still be repaired when consent returns -- a fresh marker from
        before the decline must not suppress it.
        """
        first = skill_link._ensure_retro_skill_links(may_create=_may("claude", "codex"))
        assert _result_for(first, "codex").status == "installed"

        # Repairable damage: a package-shaped link to a path that no longer
        # exists is `dangling-ours-legacy`. A `foreign` link would NOT do --
        # mm deliberately never touches an entry it does not recognize, so
        # asserting repair on one asserts the opposite of the contract.
        target = agent_targets["codex"]
        dead_pkg = skill_src.parent.parent / "gone" / "mind_meld" / "skills" / "retro_fleet"
        target.unlink()
        target.symlink_to(dead_pkg)

        # Per-row gate, not the plural one: `_skill_links_check_due` can return
        # True via the row-independent store-refresh path and would mask this.
        descriptor = skill_link._descriptor_for("codex")
        assert skill_link._skill_link_check_due_for(descriptor, _may("claude", "codex")) is True

        # And it must actually repair -- the name of this test is a promise.
        third = skill_link._ensure_retro_skill_links(may_create=_may("claude", "codex"))
        assert _result_for(third, "codex").status == "installed"
        _assert_store_link(target)


class TestInstallSkillsConsent:
    def _runner(self):
        from typer.testing import CliRunner

        return CliRunner()

    def test_declined_row_prints_remedy_and_exits_0(self, agent_targets, skill_src, _isolate_paths):
        from mind_meld.cli import app

        _write_mm_config(_isolate_paths, source_names=("claude",))
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert "Skipped:" in result.output
        assert "mm install-skills --agent codex" in result.output

    def test_all_declined_exits_0_with_distinct_message(
        self, agent_targets, skill_src, _isolate_paths
    ):
        from mind_meld.cli import app

        _write_mm_config(_isolate_paths, source_names=("claude",), maintain_links=False)
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert "No agent is enabled for skill install" in result.output
        assert "mm install-skills --agent claude" in result.output
        assert "--agent <" not in result.output
        assert "mm diag" in result.output

    def test_agent_flag_preserves_derived_grants(self, agent_targets, skill_src, _isolate_paths):
        from mind_meld import config as config_mod
        from mind_meld.cli import app
        from mind_meld.config import load_config

        _write_mm_config(_isolate_paths, source_names=("claude",))
        result = self._runner().invoke(app, ["install-skills", "--agent", "codex"])
        assert result.exit_code == 0, result.output
        loaded = load_config(config_mod.CONFIG_PATH)
        assert loaded["skills"]["maintain_links"] is True
        assert loaded["skills"]["agents"] == [
            skill_link.AGENT_ROWS[0].key,
            skill_link.AGENT_ROWS[1].key,
        ]
        assert "claude" in result.output.lower() or "Claude" in result.output

    def test_agent_flag_preserves_unknown_future_grants(
        self, agent_targets, skill_src, _isolate_paths
    ):
        from mind_meld import config as config_mod
        from mind_meld.cli import app
        from mind_meld.config import load_config

        _write_mm_config(_isolate_paths, source_names=("claude",), agents=["grok"])
        result = self._runner().invoke(app, ["install-skills", "--agent", "codex"])
        assert result.exit_code == 0, result.output
        loaded = load_config(config_mod.CONFIG_PATH)
        assert loaded["skills"]["agents"] == ["codex", "grok"]

    def test_agent_flag_when_maintain_links_was_false_writes_only_key(
        self, agent_targets, skill_src, _isolate_paths
    ):
        from mind_meld import config as config_mod
        from mind_meld.cli import app
        from mind_meld.config import load_config

        _write_mm_config(_isolate_paths, source_names=("claude",), maintain_links=False)
        result = self._runner().invoke(app, ["install-skills", "--agent", "codex"])
        assert result.exit_code == 0, result.output
        loaded = load_config(config_mod.CONFIG_PATH)
        assert loaded["skills"]["agents"] == ["codex"]

    def test_agent_flag_without_config_exits_1_and_writes_nothing(
        self, agent_targets, skill_src, _isolate_paths
    ):
        from mind_meld.cli import app

        result = self._runner().invoke(app, ["install-skills", "--agent", "codex"])
        assert result.exit_code == 1
        assert "mm init" in result.output
        assert "No agent links were changed" in result.output
        assert not agent_targets["codex"].exists()

    def test_agent_unknown_key_exits_1(self, agent_targets, skill_src, _isolate_paths):
        from mind_meld.cli import app

        _write_mm_config(_isolate_paths, source_names=("claude",))
        result = self._runner().invoke(app, ["install-skills", "--agent", "nope"])
        assert result.exit_code == 1
        assert "unknown agent 'nope'" in result.output

    def test_broken_config_fails_closed(self, agent_targets, skill_src, _isolate_paths):
        from mind_meld import config as config_mod
        from mind_meld.cli import app

        config_mod.CONFIG_PATH.write_text("this is not toml {")
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 1
        assert "could not read config" in result.output
        assert "mm: error:" in result.output
        assert "mm diag" in result.output
        assert not agent_targets["claude"].is_symlink()

    def test_agent_flag_unresolvable_source_fails_closed(
        self, agent_targets, skill_src, _isolate_paths
    ):
        from mind_meld import config as config_mod
        from mind_meld.cli import app
        from mind_meld.config import save_config

        storage = _isolate_paths / "storage"
        storage.mkdir()
        loop = _isolate_paths / "source-loop"
        loop.symlink_to(loop)
        save_config(
            {
                "device": {"id": "dev-a", "name": "Mac A"},
                "storage": {"path": str(storage)},
                "sync": {
                    "max_file_size": 1024,
                    "sources": [{"name": "claude", "path": str(loop), "type": "claude"}],
                },
            },
            config_mod.CONFIG_PATH,
        )

        result = self._runner().invoke(app, ["install-skills", "--agent", "codex"])
        assert result.exit_code == 1
        assert "could not read config" in result.output
        assert "mm: error:" in result.output
        assert "failed to resolve" in result.output
        assert "[skills]" not in config_mod.CONFIG_PATH.read_text()
        assert not agent_targets["codex"].is_symlink()

    def test_agent_flag_storage_error_exits_without_installing(
        self, agent_targets, skill_src, _isolate_paths, monkeypatch
    ):
        from mind_meld import cli as cli_module
        from mind_meld.cli import app
        from mind_meld.errors import StorageError

        _write_mm_config(_isolate_paths, source_names=("claude",))

        def fail_write(*_args, **_kwargs):
            raise StorageError("disk full")

        monkeypatch.setattr(cli_module, "patch_config_on_disk", fail_write)
        result = self._runner().invoke(app, ["install-skills", "--agent", "codex"])
        assert result.exit_code == 1
        assert "could not write" in result.output
        assert "No agent links were changed" in result.output
        assert "Retry this command" in result.output
        assert not agent_targets["codex"].is_symlink()

    def test_agent_installs_every_consented_row(self, agent_targets, skill_src, _isolate_paths):
        from mind_meld.cli import app

        _write_mm_config(_isolate_paths, source_names=("claude",))
        result = self._runner().invoke(app, ["install-skills", "--agent", "codex"])
        assert result.exit_code == 0, result.output
        assert agent_targets["claude"].is_symlink()
        assert agent_targets["codex"].is_symlink()
        assert "every authorized agent" in result.output


class TestUserRemovedLink:
    """Deletion is intent, not damage. Track 28A.

    Every key list here derives from ``AGENT_ROWS`` rather than naming agents
    literally: ``tests/test_module_boundaries.py`` AST-scans this file for a
    list of >=2 agent-key string constants and fails the build on one.
    """

    def _runner(self):
        from typer.testing import CliRunner

        return CliRunner()

    def test_push_does_not_recreate_a_link_the_user_deleted(self, agent_targets, skill_src):
        row = skill_link.AGENT_ROWS[0]
        skill_link._ensure_retro_skill_links(may_create=None)
        assert agent_targets[row.key].is_symlink()

        agent_targets[row.key].unlink()
        results = skill_link._ensure_retro_skill_links(may_create=None)

        assert not agent_targets[row.key].exists()
        assert not agent_targets[row.key].is_symlink()
        by_key = {r.descriptor.key: r for r in results}
        assert by_key[row.key].status == "removed-by-user"

    def test_fresh_machine_still_installs(self, agent_targets, skill_src):
        """No marker means mm has never been here -- install, as today."""
        row = skill_link.AGENT_ROWS[0]
        assert not skill_link._marker_exists(row.success_marker)
        results = skill_link._ensure_retro_skill_links(may_create=None)
        by_key = {r.descriptor.key: r for r in results}
        assert by_key[row.key].status == "installed"
        assert agent_targets[row.key].is_symlink()

    def test_install_skills_puts_it_back(self, agent_targets, skill_src, _isolate_paths):
        """`explicit=True` is the documented undo and skips the guard."""
        from mind_meld.cli import app

        row = skill_link.AGENT_ROWS[0]
        skill_link._ensure_retro_skill_links(may_create=None)
        agent_targets[row.key].unlink()

        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert agent_targets[row.key].is_symlink()

    def test_a_dangling_store_link_is_damage_not_a_removal(self, agent_targets, skill_src):
        """Damage keeps the link. Only an ABSENT target means intent.

        Classified with ``allow_mutate=False``: a writing run republishes the
        store before it classifies, which heals the dangle and reports
        ``unchanged``. Non-mutating is the only way to observe the
        classification itself.
        """
        row = skill_link.AGENT_ROWS[0]
        skill_link._ensure_retro_skill_links(may_create=None)
        store = skill_link._skill_store_dir()
        for child in store.iterdir():
            child.unlink()
        store.rmdir()

        results = skill_link._ensure_retro_skill_links(allow_mutate=False, may_create=None)
        by_key = {r.descriptor.key: r for r in results}
        assert by_key[row.key].status == "dangling-ours"

        healed = skill_link._ensure_retro_skill_links(may_create=None)
        assert {r.descriptor.key: r for r in healed}[row.key].status in ("installed", "unchanged")
        assert agent_targets[row.key].is_symlink()

    def test_the_drift_gate_closes_after_a_removal(self, agent_targets, skill_src):
        """G2b: the gate used to fail open on the absent lstat, forever."""
        row = skill_link.AGENT_ROWS[0]
        skill_link._ensure_retro_skill_links(may_create=None)
        agent_targets[row.key].unlink()

        descriptor = skill_link._descriptor_for(row.key)
        assert skill_link._skill_link_check_due_for(descriptor, frozenset({row.key})) is False

    def test_removal_on_one_agent_does_not_affect_another(self, agent_targets, skill_src):
        keys = [r.key for r in skill_link.AGENT_ROWS]
        skill_link._ensure_retro_skill_links(may_create=None)
        agent_targets[keys[0]].unlink()

        results = skill_link._ensure_retro_skill_links(may_create=None)
        by_key = {r.descriptor.key: r for r in results}
        assert by_key[keys[0]].status == "removed-by-user"
        for other in keys[1:]:
            assert by_key[other].status == "unchanged"
            assert agent_targets[other].is_symlink()

    def test_results_stay_complete_and_ordered(self, agent_targets, skill_src):
        skill_link._ensure_retro_skill_links(may_create=None)
        agent_targets[skill_link.AGENT_ROWS[0].key].unlink()
        results = skill_link._ensure_retro_skill_links(may_create=None)
        assert [r.descriptor.key for r in results] == [r.key for r in skill_link.AGENT_ROWS]

    def test_quiet_autopush_also_reports_user_removed(self, agent_targets, skill_src):
        row = skill_link.AGENT_ROWS[0]
        skill_link._ensure_retro_skill_links(may_create=None)
        agent_targets[row.key].unlink()
        results = skill_link._ensure_retro_skill_links(allow_mutate=False, may_create=None)
        by_key = {r.descriptor.key: r for r in results}
        assert by_key[row.key].status == "removed-by-user"
        assert not agent_targets[row.key].is_symlink()

    def test_renderer_names_the_undo(self, agent_targets, skill_src):
        row = skill_link.AGENT_ROWS[0]
        descriptor = skill_link._descriptor_for(row.key)
        result = skill_link.SkillInstallResult(descriptor, "removed-by-user")
        rendered = skill_link.render_skill_status(result)
        assert "mm install-skills" in rendered
        assert "restart the agent" in rendered

    def test_diag_distinguishes_removed_from_never_installed(self, agent_targets, skill_src):
        row = skill_link.AGENT_ROWS[0]
        rows = {r["key"]: r for r in skill_link.diagnose_skill_links()}
        assert rows[row.key]["status"] == "absent"

        skill_link._ensure_retro_skill_links(may_create=None)
        agent_targets[row.key].unlink()
        rows = {r["key"]: r for r in skill_link.diagnose_skill_links()}
        assert rows[row.key]["status"] == "removed-by-user"

    def test_removed_by_user_is_not_a_broken_status(self):
        assert "removed-by-user" not in skill_link.BROKEN_SKILL_STATUSES
        assert "absent" not in skill_link.BROKEN_SKILL_STATUSES

    def test_marker_exists_ignores_age(self, agent_targets, skill_src, monkeypatch):
        """Existence is durable; only freshness has the 24h TTL."""
        row = skill_link.AGENT_ROWS[0]
        skill_link._ensure_retro_skill_links(may_create=None)
        marker = skill_link._marker_dir() / f".{row.success_marker}"
        stale = time.time() - (skill_link.SKILL_LINK_TTL_SECONDS * 3)
        os.utime(marker, (stale, stale))

        assert skill_link._marker_is_fresh(row.success_marker) is False
        assert skill_link._marker_exists(row.success_marker) is True

    def test_a_present_target_is_never_a_removal(self, agent_targets, skill_src):
        """Only an ABSENT target can mean intent. Present-and-healthy is not."""
        row = skill_link.AGENT_ROWS[0]
        skill_link._ensure_retro_skill_links(may_create=None)
        target = agent_targets[row.key]
        assert (
            skill_link._skill_link_check_due_at(target, success_marker=row.success_marker) is False
        )

        target.unlink()
        assert (
            skill_link._skill_link_check_due_at(target, success_marker=row.success_marker) is False
        )
        assert skill_link._marker_exists(row.success_marker) is True

    def test_a_foreign_file_is_not_a_removal(self, agent_targets, skill_src):
        """The user's own file at the target stays `foreign`, never `removed-by-user`."""
        row = skill_link.AGENT_ROWS[0]
        skill_link._ensure_retro_skill_links(may_create=None)
        target = agent_targets[row.key]
        target.unlink()
        target.write_text("mine\n")

        results = skill_link._ensure_retro_skill_links(may_create=None)
        by_key = {r.descriptor.key: r for r in results}
        assert by_key[row.key].status == "foreign"
        assert target.read_text() == "mine\n"

    def test_the_gate_stays_shut_after_the_marker_goes_stale(self, agent_targets, skill_src):
        """A stale marker must not re-open the gate on a removed row.

        Caught by a ship-review mutant that the whole suite missed. Every other
        test here uses a FRESH marker, and with a fresh marker both orderings
        return False -- so moving the absent-target check below the freshness
        test passed 2741 tests while breaking the feature. The gate was then
        restructured so the absent case returns from inside its own
        `FileNotFoundError` handler, which makes that mutation unrepresentable;
        this test is what proves the restructure kept the behavior.

        The stale case is not exotic: it is every machine 24h after a removal,
        because the installer declines and therefore never touches the marker.
        Below the freshness test, the absent-target `lstat` falls into the
        blanket fail-open and the gate re-opens on every push forever for a row
        whose only possible outcome is a no-op.

        Asserts through `_skill_link_check_due_for`, not `_skill_links_check_due`
        -- the plural gate can open on the row-independent store-refresh path and
        would mask this.
        """
        row = skill_link.AGENT_ROWS[0]
        skill_link._ensure_retro_skill_links(may_create=None)
        agent_targets[row.key].unlink()

        marker = skill_link._marker_dir() / f".{row.success_marker}"
        stale = time.time() - (skill_link.SKILL_LINK_TTL_SECONDS * 3)
        os.utime(marker, (stale, stale))
        assert skill_link._marker_is_fresh(row.success_marker) is False

        descriptor = skill_link._descriptor_for(row.key)
        assert skill_link._skill_link_check_due_for(descriptor, frozenset({row.key})) is False

    def test_an_lstat_error_is_not_a_removal(self, agent_targets, skill_src, monkeypatch):
        """An inspection failure is not consent to stop maintaining a link.

        The gate must distinguish FileNotFoundError (the link is gone) from any
        other OSError (we could not look). Fail-closed here would silently
        suppress repair on an EACCES agent directory.
        """
        row = skill_link.AGENT_ROWS[0]
        skill_link._ensure_retro_skill_links(may_create=None)
        target = agent_targets[row.key]
        target.unlink()

        original = Path.lstat

        def fake_lstat(self, *args, **kwargs):
            if self.name == "retro-fleet":
                raise PermissionError("simulated EACCES")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", fake_lstat)
        assert (
            skill_link._skill_link_check_due_at(target, success_marker=row.success_marker) is True
        )

    def test_push_is_silent_about_a_removal(self, agent_targets, skill_src, capsys):
        """A deliberate removal is not a fault, so it must not spend a notice."""
        row = skill_link.AGENT_ROWS[0]
        skill_link._ensure_retro_skill_links(may_create=None)
        agent_targets[row.key].unlink()
        capsys.readouterr()

        skill_link._ensure_retro_skill_links(may_create=None)
        assert "mm: notice:" not in capsys.readouterr().err

    def test_removal_survives_a_store_version_bump(self, agent_targets, skill_src, monkeypatch):
        """The realistic resurrection path: every mm release re-opens the gate.

        `_store_needs_refresh` is row-independent, so a version bump opens the
        plural gate for reasons that have nothing to do with this row. The
        per-row guard is what must still hold.
        """
        row = skill_link.AGENT_ROWS[0]
        skill_link._ensure_retro_skill_links(may_create=None)
        agent_targets[row.key].unlink()

        monkeypatch.setattr(skill_link, "__version__", "99.99.99")
        assert skill_link._skill_links_check_due(may_create=None) is True

        results = skill_link._ensure_retro_skill_links(may_create=None)
        assert {r.descriptor.key: r for r in results}[row.key].status == "removed-by-user"
        assert not agent_targets[row.key].exists()
        assert not agent_targets[row.key].is_symlink()

    def test_install_skills_reports_removed_by_user_without_failing(
        self, agent_targets, skill_src, _isolate_paths, monkeypatch
    ):
        """Defensive branch pin.

        `install_skills_cmd` always passes `explicit=True`, so the guard never
        fires and this status is unreachable there today. The branch exists
        because the bare `else` below it reports "installation failed: None" and
        exits 1 -- a future non-explicit caller would turn a benign outcome into
        a hard failure. Pinned so that stays true.
        """
        from mind_meld.cli import app

        descriptor = skill_link._descriptor_for(skill_link.AGENT_ROWS[0].key)
        monkeypatch.setattr(
            skill_link,
            "_ensure_retro_skill_links",
            lambda **_kwargs: (skill_link.SkillInstallResult(descriptor, "removed-by-user"),),
        )
        result = self._runner().invoke(app, ["install-skills"])
        assert result.exit_code == 0, result.output
        assert "Left removed" in result.output
        assert "installation failed" not in result.output

    def test_shell_completion_options_are_exposed(self):
        """`add_completion=True` is a user-facing surface added by this Track."""
        from typer.testing import CliRunner

        from mind_meld.cli import app

        output = " ".join(CliRunner().invoke(app, ["--help"]).output.split())
        assert "--install-completion" in output
        assert "--show-completion" in output
