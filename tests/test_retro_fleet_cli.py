"""Tests for the ``mm retro-fleet`` typer wrapper (cli.py:retro_fleet_cmd).

The wrapper is a thin shim that lazy-imports ``aggregator.main`` and
forwards typer args. Pin: positional window arg, default-7d behavior,
``--no-author-filter`` flag forwarding, and exit-code propagation.
Routing through ``mm`` (instead of ``python -m
mind_meld.skills.retro_fleet.aggregator``) is the load-bearing fix for
v0.11.22 — see docs/invariants/events-retro.md "mm retro-fleet [window]
typer wrapper".
"""

from __future__ import annotations

from typer.testing import CliRunner


class TestRetroFleetCommand:
    def _runner(self) -> CliRunner:
        return CliRunner()

    def test_default_window_is_7d(self, monkeypatch):
        from mind_meld.cli import app

        captured: dict = {}

        def _fake_main(argv):
            captured["argv"] = list(argv)
            return 0

        monkeypatch.setattr("mind_meld.skills.retro_fleet.aggregator.main", _fake_main)
        result = self._runner().invoke(app, ["retro-fleet"])
        assert result.exit_code == 0, result.output
        assert captured["argv"] == ["7d"]

    def test_explicit_window_forwarded(self, monkeypatch):
        from mind_meld.cli import app

        captured: dict = {}

        def _fake_main(argv):
            captured["argv"] = list(argv)
            return 0

        monkeypatch.setattr("mind_meld.skills.retro_fleet.aggregator.main", _fake_main)
        result = self._runner().invoke(app, ["retro-fleet", "30d"])
        assert result.exit_code == 0, result.output
        assert captured["argv"] == ["30d"]

    def test_dump_host_usage_flag_forwarded(self, monkeypatch):
        from mind_meld.cli import app

        captured: dict = {}

        def _fake_main(argv):
            captured["argv"] = list(argv)
            return 0

        monkeypatch.setattr("mind_meld.skills.retro_fleet.aggregator.main", _fake_main)
        result = self._runner().invoke(app, ["retro-fleet", "7d", "--dump-host-usage"])
        assert result.exit_code == 0, result.output
        assert captured["argv"] == ["7d", "--dump-host-usage"]

    def test_no_author_filter_flag_forwarded(self, monkeypatch):
        from mind_meld.cli import app

        captured: dict = {}

        def _fake_main(argv):
            captured["argv"] = list(argv)
            return 0

        monkeypatch.setattr("mind_meld.skills.retro_fleet.aggregator.main", _fake_main)
        result = self._runner().invoke(app, ["retro-fleet", "14d", "--no-author-filter"])
        assert result.exit_code == 0, result.output
        assert captured["argv"] == ["14d", "--no-author-filter"]

    def test_aggregator_nonzero_exit_propagates(self, monkeypatch):
        from mind_meld.cli import app

        monkeypatch.setattr(
            "mind_meld.skills.retro_fleet.aggregator.main",
            lambda argv: 2,
        )
        result = self._runner().invoke(app, ["retro-fleet", "7d"])
        assert result.exit_code == 2

    def test_command_visible_in_help(self):
        """The command is intentionally NOT hidden — matches the
        ``autopull`` / ``autopush`` / ``install-skills`` precedent of
        listing 'designed for Claude Code' commands in --help so they
        stay discoverable for debugging and direct power-user use."""
        from mind_meld.cli import app

        result = self._runner().invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "retro-fleet" in result.output

    def test_theme_and_noteworthy_forwarded(self, monkeypatch):
        """v0.12.0 — second-pass card flags forward verbatim through the
        typer wrapper. Pin: ``--theme`` repeats, ``--noteworthy``,
        ``--name``, and ``--no-save`` all reach the aggregator argv."""
        from mind_meld.cli import app

        captured: dict = {}

        def _fake_main(argv):
            captured["argv"] = list(argv)
            return 0

        monkeypatch.setattr("mind_meld.skills.retro_fleet.aggregator.main", _fake_main)
        result = self._runner().invoke(
            app,
            [
                "retro-fleet",
                "7d",
                "--theme",
                "alpha",
                "--theme",
                "beta",
                "--noteworthy",
                "shipped",
                "--name",
                "kb",
                "--no-save",
            ],
        )
        assert result.exit_code == 0, result.output
        argv = captured["argv"]
        # Flags forward in the same shape the aggregator's argparse expects.
        assert "--theme" in argv
        assert "alpha" in argv
        assert "beta" in argv
        assert "--noteworthy" in argv
        assert "shipped" in argv
        assert "--name" in argv
        assert "kb" in argv
        assert "--no-save" in argv

    def test_no_save_is_accepted_as_deprecated_noop(self, monkeypatch):
        """Hidden flag still forwards so a stale SKILL.md Step 4 exits 0."""
        from mind_meld.cli import app

        captured: dict = {}

        def _fake_main(argv):
            captured["argv"] = list(argv)
            return 0

        monkeypatch.setattr("mind_meld.skills.retro_fleet.aggregator.main", _fake_main)
        result = self._runner().invoke(app, ["retro-fleet", "30d", "--no-save"])
        assert result.exit_code == 0, result.output
        assert captured["argv"] == ["30d", "--no-save"]
        help_result = self._runner().invoke(app, ["retro-fleet", "--help"])
        # hidden=True: the Options list does not advertise the flag. The
        # command docstring may still name it so a stale SKILL.md is explained.
        options = help_result.output.split("Options")[-1]
        assert "--theme" in options
        assert "--no-save" not in options
