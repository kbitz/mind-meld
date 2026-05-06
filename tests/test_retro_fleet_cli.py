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
