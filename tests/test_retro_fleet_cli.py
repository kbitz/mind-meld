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

import json
import os
import re
import subprocess
import sys

import pytest
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

    def test_dump_host_usage_visible_in_command_help(self):
        """v0.12.49 un-hid the flag; this is the pin, and the exact inverse of
        the ``--no-save`` assertion below.

        STRIP ANSI FIRST. Rich renders a colored option name as several
        separately-styled runs — ``ESC[1;36m-ESC[0mESC[1;36m-dumpESC[0m
        ESC[1;36m-host-usageESC[0m`` — so the raw token is not in
        ``result.output`` at all when color is on. It is on in GitHub Actions
        and off on a piped local run, which is precisely the shape that passes
        for the author and fails in CI.
        """
        from mind_meld.cli import app

        result = self._runner().invoke(app, ["retro-fleet", "--help"])
        assert result.exit_code == 0
        plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.output)
        options = plain.rsplit("Options", 1)[-1]
        assert "--dump-host-usage" in options

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
                "user",
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
        assert "user" in argv
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
        assert help_result.exit_code == 0, help_result.output
        # hidden=True: the Options list does not advertise the flag. The
        # command docstring may still name it so a stale SKILL.md is explained.
        plain_help = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", help_result.output)
        options = plain_help.rsplit("Options", 1)[-1]
        assert "--theme" in options
        assert "--no-save" not in options


class TestPricingFlags:
    def test_prices_and_mark_forwarded(self, monkeypatch):
        from mind_meld.cli import app

        captured = []
        monkeypatch.setattr(
            "mind_meld.skills.retro_fleet.aggregator.main", lambda argv: captured.extend(argv) or 0
        )
        result = CliRunner().invoke(
            app,
            [
                "retro-fleet",
                "30d",
                "--set-model-price",
                "new-model=1,0,0,2",
                "--set-model-price",
                "another=2,0,0,3",
                "--mark-pricing-verified",
            ],
        )
        assert result.exit_code == 0, result.output
        assert captured == [
            "30d",
            "--set-model-price=new-model=1,0,0,2",
            "--set-model-price=another=2,0,0,3",
            "--mark-pricing-verified",
        ]

    @pytest.mark.parametrize(
        "spec",
        ["bad\n\x1b[31m=1,0,0,2", "new=nan,0,0,2", "new=1,2,3", "new=1001,0,0,2", "-bad=1,0,0,2"],
    )
    def test_bad_spec_exits_two_sanitized_and_never_writes(self, spec):
        from mind_meld import token_usage as tu
        from mind_meld.cli import app

        result = CliRunner().invoke(app, ["retro-fleet", "--set-model-price=" + spec])
        assert result.exit_code == 2, result.output
        assert "invalid --set-model-price" in result.output
        assert "Fix: mm retro-fleet" in result.output
        assert "\x1b" not in result.output
        assert not tu.PRICING_CACHE_PATH.exists()

    def test_write_failure_retry_reconstructs_requested_specs(self, monkeypatch):
        from mind_meld import token_usage as tu
        from mind_meld.cli import app

        def fail(*args, **kwargs):
            raise OSError("read-only cache")

        monkeypatch.setattr(tu, "update_pricing_cache", fail)
        result = CliRunner().invoke(
            app,
            [
                "retro-fleet",
                "--set-model-price",
                "new-model=1,.2,0,3",
                "--set-model-price",
                "new-model=9,9,9,9",
                "--set-model-price",
                "other-model=2,0.1,2.5,10",
            ],
        )
        assert result.exit_code == 1, result.output
        assert "Fix: mm retro-fleet 7d --set-model-price new-model=9.0,9.0,9.0,9.0" in result.output
        assert "--set-model-price other-model=2.0,0.1,2.5,10.0" in result.output
        assert result.output.count("new-model=") == 1

    def test_too_many_model_prices_exit_two(self):
        from mind_meld.cli import app

        specs = []
        for i in range(129):
            specs += ["--set-model-price", f"model-{i}=1,1,1,1"]
        result = CliRunner().invoke(app, ["retro-fleet", *specs])
        assert result.exit_code == 2, result.output
        assert "too many --set-model-price" in result.output

    def test_write_failure_exits_one_and_sanitizes(self, monkeypatch):
        from mind_meld import token_usage as tu
        from mind_meld.cli import app

        def fail(*args, **kwargs):
            raise OSError("read-only\n\x1b[31mcache")

        monkeypatch.setattr(tu, "update_pricing_cache", fail)
        result = CliRunner().invoke(app, ["retro-fleet", "--mark-pricing-verified"])
        assert result.exit_code == 1, result.output
        assert "could not write pricing cache" in result.output
        assert "Fix: mm retro-fleet 7d --mark-pricing-verified" in result.output
        assert "\x1b" not in result.output
        assert "# Retro" not in result.output

    def test_corrupt_cache_write_failure_names_rm_fix_with_quoted_path(self, monkeypatch, tmp_path):
        import shlex

        from mind_meld import token_usage as tu
        from mind_meld.cli import app

        hostile = tmp_path / "a b'c" / "pricing-cache.json"
        hostile.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(tu, "PRICING_CACHE_PATH", hostile)
        tu.PRICING_CACHE_PATH.write_text("{broken json")
        tu._PRICING_CACHE_MEMO = None
        result = CliRunner().invoke(app, ["retro-fleet", "--mark-pricing-verified"])
        assert result.exit_code == 1, result.output
        assert f"Fix: rm -- {shlex.quote(str(tu.PRICING_CACHE_PATH))}" in result.output
        assert "\x1b" not in result.output
        assert "# Retro" not in result.output

    def test_subprocess_empty_pricing_cache_path_env_uses_home_default(self, tmp_path):
        events = tmp_path / "events"
        events.mkdir()
        env = {
            **os.environ,
            "MM_PRICING_CACHE_PATH": "",
            "MM_EVENTS_DIR": str(events),
            "HOME": str(tmp_path),
        }
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mind_meld.cli",
                "retro-fleet",
                "7d",
                "--no-author-filter",
                "--set-model-price",
                "new-model=1,.2,0,3",
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert (
            "Pricing: saved new-model to this machine's pricing cache (default location).\n"
            in result.stderr
        )
        assert "Pricing:" not in result.stdout
        saved = json.loads((tmp_path / ".config/mind-meld/pricing-cache.json").read_text())
        assert saved["models"]["new-model"]["input"] == 1

    def test_subprocess_pricing_override_is_hermetic(self, tmp_path):
        from mind_meld import token_usage as tu

        cache = tmp_path / "subprocess-prices.json"
        events = tmp_path / "events"
        events.mkdir()
        env = {
            **os.environ,
            "MM_PRICING_CACHE_PATH": str(cache),
            "MM_EVENTS_DIR": str(events),
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        }
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mind_meld.cli",
                "retro-fleet",
                "7d",
                "--no-author-filter",
                "--set-model-price",
                "new-model=1,.2,0,3",
                "--mark-pricing-verified",
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "Pricing: saved new-model and marked verified " in result.stderr
        assert "in this machine's pricing cache (set by MM_PRICING_CACHE_PATH).\n" in result.stderr
        assert "Pricing:" not in result.stdout
        saved = json.loads(cache.read_text())
        assert saved["models"]["new-model"]["input"] == 1
        assert saved["models"]["new-model"]["verified_date"] == saved["verified_date"]
        assert not tu.PRICING_CACHE_PATH.exists()

    def test_set_model_price_confirmation_stderr_and_unchanged_stdout(
        self, tmp_path, monkeypatch, capsys
    ):
        from mind_meld.skills.retro_fleet import aggregator

        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setenv("MM_EVENTS_DIR", str(events_dir))

        rc = aggregator.main(
            [
                "7d",
                "--no-author-filter",
                "--set-model-price",
                "model-b=1,.2,0,3",
                "--set-model-price",
                "model-a=2,.1,.25,4",
            ]
        )
        first = capsys.readouterr()
        assert rc == 0

        rc2 = aggregator.main(["7d", "--no-author-filter"])
        second = capsys.readouterr()
        assert rc2 == 0

        expected_line = (
            "Pricing: saved model-a, model-b to this machine's pricing cache "
            "(set by MM_PRICING_CACHE_PATH).\n"
        )
        assert first.out == second.out
        assert "Pricing:" not in first.out
        assert first.err == expected_line + second.err

    def test_mark_pricing_verified_confirmation_stderr_and_unchanged_stdout(
        self, tmp_path, monkeypatch, capsys
    ):
        from mind_meld import token_usage as tu
        from mind_meld.skills.retro_fleet import aggregator

        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setenv("MM_EVENTS_DIR", str(events_dir))

        rc = aggregator.main(["7d", "--no-author-filter", "--mark-pricing-verified"])
        first = capsys.readouterr()
        assert rc == 0

        rc2 = aggregator.main(["7d", "--no-author-filter"])
        second = capsys.readouterr()
        assert rc2 == 0

        saved = json.loads(tu.PRICING_CACHE_PATH.read_text())
        stamped = saved["verified_date"]
        expected_line = (
            f"Pricing: marked verified {stamped} in this machine's pricing cache "
            "(set by MM_PRICING_CACHE_PATH).\n"
        )
        assert first.out == second.out
        assert "Pricing:" not in first.out
        assert first.err == expected_line + second.err

    def test_combined_set_and_mark_verified_emits_single_confirmation_line(
        self, tmp_path, monkeypatch, capsys
    ):
        from mind_meld import token_usage as tu
        from mind_meld.skills.retro_fleet import aggregator

        events_dir = tmp_path / "events"
        events_dir.mkdir()
        monkeypatch.setenv("MM_EVENTS_DIR", str(events_dir))

        rc = aggregator.main(
            [
                "7d",
                "--no-author-filter",
                "--set-model-price",
                "new-model=1,.2,0,3",
                "--mark-pricing-verified",
            ]
        )
        first = capsys.readouterr()
        assert rc == 0

        rc2 = aggregator.main(["7d", "--no-author-filter"])
        second = capsys.readouterr()
        assert rc2 == 0

        saved = json.loads(tu.PRICING_CACHE_PATH.read_text())
        stamped = saved["verified_date"]
        expected_line = (
            f"Pricing: saved new-model and marked verified {stamped} in this machine's "
            "pricing cache (set by MM_PRICING_CACHE_PATH).\n"
        )
        assert first.out == second.out
        assert "Pricing:" not in first.out
        assert first.err == expected_line + second.err

    def test_aggregator_main_write_failure_keeps_exact_error_without_confirmation(
        self, monkeypatch, capsys
    ):
        from mind_meld import token_usage as tu
        from mind_meld.skills.retro_fleet import aggregator

        def fail(*args, **kwargs):
            raise OSError("read-only cache")

        monkeypatch.setattr(tu, "update_pricing_cache", fail)
        rc = aggregator.main(["7d", "--mark-pricing-verified"])
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.out == ""
        assert "Pricing:" not in captured.err
        assert captured.err == (
            f"mm: could not write pricing cache at {tu.PRICING_CACHE_PATH}: read-only cache. "
            "Fix: mm retro-fleet 7d --mark-pricing-verified\n"
        )
