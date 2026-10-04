"""Tests for v0.10.0 per-machine source toggle CLI commands + display.

Covers:
- `mm enable-source` / `mm disable-source` / `mm reconfigure-sources` happy paths
- `--force` escape hatch + closest-match hint on unknown names
- Idempotent no-op messages
- `mm sources` Enabled column + dim disabled rows + shows-disabled-too
- `mm status` disabled-sources breadcrumb + new-source hint (one-shot)

Consumer-boundary tombstone-suppression invariants (P0) live in
`tests/test_integration.py::TestDisabledSourcesTombstoneSuppression`.
"""

from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from mind_meld import seen_sources
from mind_meld.cli import (
    _filter_disabled_sources,
    _filter_unselected_sources,
    _known_source_names,
    _prompt_source_toggle,
    _validate_source_name,
    app,
)
from mind_meld.config import get_default_source, save_config
from mind_meld.errors import ConfigError

runner = CliRunner()


@pytest.fixture
def isolated_seen_sources(tmp_path, monkeypatch):
    monkeypatch.setattr("mind_meld.seen_sources.SEEN_DIR", tmp_path / "mm_state")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Minimal config with claude + gstack explicit sources, ready to mutate."""
    storage = tmp_path / "storage"
    storage.mkdir()
    claude = tmp_path / ".claude"
    claude.mkdir()
    gstack = tmp_path / ".gstack"
    gstack.mkdir()
    config_path = tmp_path / "config.toml"
    config = {
        "device": {"id": "abc123", "name": "MacBook"},
        "storage": {"path": str(storage)},
        "sync": {
            "max_file_size": 52_428_800,
            "sources": [
                {"name": "claude", "path": str(claude), "type": "claude"},
                {"name": "gstack", "path": str(gstack), "type": "generic"},
            ],
        },
        "crypto": {"argon2_memory_kb": 1024},
    }
    save_config(config, config_path)
    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
    return config_path


# ── _validate_source_name + _known_source_names ──────────────────────


class TestValidateSourceName:
    def test_known_name_passes(self):
        cfg = {"sync": {"sources": [{"name": "claude", "path": "/", "type": "claude"}]}}
        _validate_source_name("claude", cfg, force=False)
        _validate_source_name("gstack", cfg, force=False)  # in DEFAULT_SOURCES

    def test_unknown_name_strict_raises(self):
        cfg = {"sync": {"sources": []}}
        with pytest.raises(ConfigError, match="unknown source 'future-agent'"):
            _validate_source_name("future-agent", cfg, force=False)

    def test_unknown_name_force_warns_and_passes(self, capsys):
        cfg = {"sync": {"sources": []}}
        _validate_source_name("future-agent", cfg, force=True)
        captured = capsys.readouterr()
        assert "future-agent" in captured.err
        assert "--force" in captured.err

    def test_closest_match_hint_in_error(self):
        cfg = {"sync": {"sources": []}}
        with pytest.raises(ConfigError, match="Did you mean 'gstack'"):
            _validate_source_name("gstck", cfg, force=False)

    def test_known_source_names_returns_sorted_union(self):
        cfg = {"sync": {"sources": [{"name": "custom", "path": "/", "type": "claude"}]}}
        names = _known_source_names(cfg)
        assert "custom" in names
        assert "claude" in names
        assert "gstack" in names
        assert names == sorted(names)


# ── _prompt_source_toggle ────────────────────────────────────────────


class TestPromptSourceToggle:
    def test_default_reflects_current_state_true(self, monkeypatch, tmp_path):
        """Empty stdin = accept default. current_state=True → returns True."""
        captured: dict = {}

        def fake_confirm(prompt, default):
            captured["prompt"] = prompt
            captured["default"] = default
            return default

        monkeypatch.setattr(typer, "confirm", fake_confirm)
        result = _prompt_source_toggle(
            {"name": "claude", "path": str(tmp_path)}, current_state=True
        )
        assert result is True
        assert captured["default"] is True
        assert "claude" in captured["prompt"]

    def test_default_reflects_current_state_false(self, monkeypatch, tmp_path):
        def fake_confirm(prompt, default):
            return default

        monkeypatch.setattr(typer, "confirm", fake_confirm)
        result = _prompt_source_toggle(
            {"name": "gstack", "path": str(tmp_path)}, current_state=False
        )
        assert result is False


# ── disable-source ────────────────────────────────────────────────────


class TestDisableSource:
    def test_disables_known_source(self, cfg, isolated_seen_sources):
        result = runner.invoke(app, ["disable-source", "gstack"])
        assert result.exit_code == 0, result.output
        assert "Disabled source 'gstack'" in result.output

        import tomllib

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert on_disk["sync"]["disabled_sources"] == ["gstack"]

    def test_idempotent_no_op_on_already_disabled(self, cfg, isolated_seen_sources):
        runner.invoke(app, ["disable-source", "gstack"])
        result = runner.invoke(app, ["disable-source", "gstack"])
        assert result.exit_code == 0
        assert "already disabled" in result.output

    def test_unknown_name_strict_errors(self, cfg, isolated_seen_sources):
        result = runner.invoke(app, ["disable-source", "definitely-unknown"])
        assert result.exit_code != 0
        assert "unknown source" in result.output

    def test_closest_match_hint(self, cfg, isolated_seen_sources):
        result = runner.invoke(app, ["disable-source", "gstck"])
        assert result.exit_code != 0
        # Rich text-wraps the error across lines once the valid-source list
        # gets long (mm-events addition v0.11.0). Match across the wrap.
        normalized = " ".join(result.output.split())
        assert "Did you mean 'gstack'" in normalized

    def test_unknown_name_force_accepted(self, cfg, isolated_seen_sources):
        """Forward-compat: pre-disable codex before it ships."""
        result = runner.invoke(app, ["disable-source", "codex", "--force"])
        assert result.exit_code == 0
        assert "Disabled" in result.output
        # stderr breadcrumb in CliRunner: combined output has the warning.
        # We assert the field landed.
        import tomllib

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert "codex" in on_disk["sync"]["disabled_sources"]

    def test_records_seen(self, cfg, isolated_seen_sources):
        runner.invoke(app, ["disable-source", "gstack"])
        assert seen_sources.seen_path().exists()
        seen = json.loads(seen_sources.seen_path().read_text())
        assert "gstack" in seen


# ── enable-source ─────────────────────────────────────────────────────


class TestEnableSource:
    def test_enables_disabled_source(self, cfg, isolated_seen_sources):
        runner.invoke(app, ["disable-source", "gstack"])
        result = runner.invoke(app, ["enable-source", "gstack"])
        assert result.exit_code == 0, result.output
        assert "Enabled source 'gstack'" in result.output

        import tomllib

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert on_disk["sync"]["disabled_sources"] == []

    def test_already_enabled_no_op(self, cfg, isolated_seen_sources):
        # gstack is in explicit sources, not disabled.
        result = runner.invoke(app, ["enable-source", "gstack"])
        assert result.exit_code == 0
        assert "already enabled" in result.output

    def test_unknown_name_strict_errors(self, cfg, isolated_seen_sources):
        result = runner.invoke(app, ["enable-source", "definitely-unknown"])
        assert result.exit_code != 0
        assert "unknown source" in result.output

    def test_enable_source_refuses_opencode_after_migration(self, cfg, isolated_seen_sources):
        """After Track 37B, opencode is an ordinary unknown name — not a
        not-yet-shipped source, and --force is not offered as a way in."""
        result = runner.invoke(app, ["enable-source", "opencode"])
        assert result.exit_code != 0
        combined = " ".join((result.output + (result.stderr or "")).split())
        assert "unknown source 'opencode'" in combined
        assert "retired in v0.12.55" in combined
        assert "not yet shipped" not in combined
        assert "--force" not in combined

    def test_unknown_name_force_accepted(self, cfg, isolated_seen_sources):
        # First pre-disable codex via --force.
        runner.invoke(app, ["disable-source", "codex", "--force"])
        # Now re-enable via --force (codex still not in DEFAULT_SOURCES on this
        # mm version, so --force needed for both directions).
        result = runner.invoke(app, ["enable-source", "codex", "--force"])
        assert result.exit_code == 0
        import tomllib

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert "codex" not in (on_disk["sync"].get("disabled_sources") or [])

    def test_appends_default_for_known_default_not_in_explicit(
        self, tmp_path, monkeypatch, isolated_seen_sources
    ):
        """If user has explicit [[sync.sources]] without gstack, but gstack is
        in DEFAULT_SOURCES, `mm enable-source gstack` appends gstack's default
        config so the source actually starts syncing."""
        storage = tmp_path / "storage"
        storage.mkdir()
        claude = tmp_path / ".claude"
        claude.mkdir()
        config_path = tmp_path / "config.toml"
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(storage)},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(claude), "type": "claude"},
                ],
            },
            "crypto": {"argon2_memory_kb": 1024},
        }
        save_config(config, config_path)
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)

        result = runner.invoke(app, ["enable-source", "gstack"])
        assert result.exit_code == 0, result.output
        import tomllib

        with open(config_path, "rb") as f:
            on_disk = tomllib.load(f)
        names = [s["name"] for s in on_disk["sync"]["sources"]]
        assert "gstack" in names

    @pytest.mark.parametrize("source_name", ["codex", "grok"])
    def test_appends_exact_agent_default_for_explicit_legacy_config(
        self, cfg, isolated_seen_sources, source_name
    ):
        """Enabling a newly shipped agent source must add its curated scope.

        Existing installations use explicit source lists, so this is the
        compatibility path that turns `mm enable-source` into real syncing.
        Pin the whole default entry: broadening it could sync credentials or
        session state, while narrowing it would lose agent configuration.
        """
        result = runner.invoke(app, ["enable-source", source_name])
        assert result.exit_code == 0, result.output

        import tomllib

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        added = next(s for s in on_disk["sync"]["sources"] if s["name"] == source_name)
        assert added == get_default_source(source_name)


class TestCursorUsageConsent:
    @pytest.mark.parametrize("enabled", [True, False])
    def test_cursor_toggle_changes_only_usage_consent(self, cfg, isolated_seen_sources, enabled):
        import tomllib

        from mind_meld import host_usage

        before = tomllib.loads(cfg.read_text())
        before["retro"] = {"cursor_host_usage": not enabled, "author_emails": ["test@example.com"]}
        save_config(before, cfg)
        foreign = {"command": "existing-hook"}
        hooks = [foreign] if enabled else [foreign, host_usage.CURSOR_HOOK_ENTRY]
        host_usage.CURSOR_HOOK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        host_usage.CURSOR_HOOK_CONFIG_PATH.write_text(
            json.dumps({"version": 1, "hooks": {"stop": hooks}})
        )
        command = "enable-source" if enabled else "disable-source"
        result = runner.invoke(app, [command, "cursor"])
        assert result.exit_code == 0, result.output
        after = tomllib.loads(cfg.read_text())
        assert after["retro"]["cursor_host_usage"] is enabled
        assert after["retro"]["author_emails"] == ["test@example.com"]
        assert after["sync"] == before["sync"]
        assert "usage capture" in result.output
        assert "--force" not in result.output
        assert not seen_sources.seen_path().exists()
        assert host_usage.cursor_hook_state() == ("installed" if enabled else "missing")
        stop = json.loads(host_usage.CURSOR_HOOK_CONFIG_PATH.read_text())["hooks"]["stop"]
        assert stop[0] == foreign

        original = cfg.read_bytes()
        hooks_before = host_usage.CURSOR_HOOK_CONFIG_PATH.read_bytes()
        again = runner.invoke(app, [command, "cursor"])
        assert again.exit_code == 0, again.output
        assert "already" in again.output
        assert "hook" not in again.output
        assert cfg.read_bytes() == original
        assert host_usage.CURSOR_HOOK_CONFIG_PATH.read_bytes() == hooks_before

    def test_reenable_repairs_and_reports_a_missing_hook(self, cfg, isolated_seen_sources):
        from mind_meld import host_usage

        assert runner.invoke(app, ["enable-source", "cursor"]).exit_code == 0
        host_usage.CURSOR_HOOK_CONFIG_PATH.write_text(json.dumps({"version": 1, "hooks": {}}))
        result = runner.invoke(app, ["enable-source", "cursor"])
        assert result.exit_code == 0, result.output
        assert "already enabled; reinstalled its hook" in " ".join(result.output.split())
        assert host_usage.cursor_hook_state() == "installed"

    @pytest.mark.parametrize("shape", ["symlink", "version2", "malformed"])
    def test_disable_revokes_consent_when_hooks_file_is_unusable(
        self, cfg, tmp_path, isolated_seen_sources, shape
    ):
        import tomllib

        from mind_meld import host_usage

        config = tomllib.loads(cfg.read_text())
        config["retro"] = {"cursor_host_usage": True}
        save_config(config, cfg)
        path = host_usage.CURSOR_HOOK_CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        if shape == "symlink":
            target = tmp_path / "dotfiles-hooks.json"
            target.write_text(
                json.dumps({"version": 1, "hooks": {"stop": [host_usage.CURSOR_HOOK_ENTRY]}})
            )
            path.symlink_to(target)
        elif shape == "version2":
            path.write_text(json.dumps({"version": 2, "hooks": {}}))
        else:
            path.write_bytes(b"not json")
        before = path.read_bytes()
        result = runner.invoke(app, ["disable-source", "cursor"])
        assert result.exit_code == 0, result.output
        assert tomllib.loads(cfg.read_text())["retro"]["cursor_host_usage"] is False
        assert "could not be removed" in " ".join(result.stderr.split())
        assert path.read_bytes() == before
        assert path.is_symlink() is (shape == "symlink")

    def test_enable_hook_failure_grants_no_consent(self, cfg, isolated_seen_sources):
        import tomllib

        from mind_meld import host_usage

        host_usage.CURSOR_HOOK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        host_usage.CURSOR_HOOK_CONFIG_PATH.write_bytes(b"not json")
        result = runner.invoke(app, ["enable-source", "cursor"])
        assert result.exit_code == 1
        assert "completion hook" in result.stderr
        assert tomllib.loads(cfg.read_text()).get("retro", {}).get("cursor_host_usage") is None

    def test_existing_custom_cursor_source_retains_file_toggle(self, cfg, isolated_seen_sources):
        import tomllib

        before = tomllib.loads(cfg.read_text())
        before["sync"]["sources"].append(
            {"name": "cursor", "path": str(cfg.parent), "type": "generic"}
        )
        save_config(before, cfg)
        result = runner.invoke(app, ["disable-source", "cursor"])
        assert result.exit_code == 0, result.output
        after = tomllib.loads(cfg.read_text())
        assert after["sync"]["disabled_sources"] == ["cursor"]
        assert after.get("retro", {}).get("cursor_host_usage") is not True

    def test_unconsented_hook_is_write_free_and_does_not_block_cursor(self, cfg):
        from mind_meld import host_usage

        result = runner.invoke(
            app, ["capture-cursor-usage"], input='{"hook_event_name":"stop","status":"completed"}'
        )
        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == "{}"
        assert not host_usage.CURSOR_CACHE_PATH.exists()

    @pytest.mark.parametrize(
        "data", [b"x" * 65_537, b"\xff", b"[" * 1_200 + b"]" * 1_200], ids=["big", "bytes", "deep"]
    )
    def test_unreadable_hook_input_never_blocks_cursor(self, cfg, data):
        result = runner.invoke(app, ["capture-cursor-usage"], input=data)
        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == "{}"
        assert "unreadable" in result.stderr

    def test_capture_failure_does_not_block_cursor_or_expose_payload(self, cfg):
        import tomllib

        from mind_meld import host_usage

        config = tomllib.loads(cfg.read_text())
        config["retro"] = {"cursor_host_usage": True}
        save_config(config, cfg)
        result = runner.invoke(
            app,
            ["capture-cursor-usage"],
            input=json.dumps(
                {
                    "hook_event_name": "stop",
                    "status": "completed",
                    "text": "PRIVATE RESPONSE",
                    "model": "PRIVATE MODEL",
                }
            ),
        )
        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == "{}"
        assert "could not be recorded" in result.stderr
        assert "PRIVATE" not in result.output
        assert not host_usage.CURSOR_CACHE_PATH.exists()

    def _fake_cursor_agent(self, tmp_path, monkeypatch, *, exit_code=0):
        payload = {"type": "result", "subtype": "success", "result": "OK"}
        raw = json.dumps(payload) + "\n"
        executable = tmp_path / "cursor-agent"
        executable.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$ARGV_LOG"\n'
            "cat <<'JSON'\n" + raw + f"JSON\nexit {exit_code}\n"
        )
        executable.chmod(0o700)
        monkeypatch.setenv("PATH", str(tmp_path) + ":" + __import__("os").environ["PATH"])
        monkeypatch.setenv("ARGV_LOG", str(tmp_path / "argv"))
        return raw, lambda: (tmp_path / "argv").read_text().splitlines()

    def _consent(self, cfg):
        import tomllib

        config = tomllib.loads(cfg.read_text())
        config["retro"] = {"cursor_host_usage": True}
        save_config(config, cfg)

    @pytest.mark.parametrize(
        "flags,child",
        [
            ([], ["--output-format=json", "-p", "hello"]),
            (["--output-format=text"], ["-p", "--output-format=json", "hello"]),
            (["--output-format", "text"], ["-p", "--output-format", "json", "hello"]),
            (["--output-format=json"], ["-p", "--output-format=json", "hello"]),
            (["--output-format=stream-json"], ["-p", "--output-format=stream-json", "hello"]),
        ],
    )
    def test_wrapper_output_formats(self, cfg, tmp_path, monkeypatch, flags, child):
        self._consent(cfg)
        raw, argv = self._fake_cursor_agent(tmp_path, monkeypatch)
        result = runner.invoke(app, ["cursor-agent", "-p", *flags, "hello"])
        assert result.exit_code == 0, result.output
        renders_text = not flags or "text" in flags[-1]
        assert result.stdout == ("OK\n" if renders_text else raw)
        assert argv() == child

    def test_wrapper_keeps_text_after_separator_positional(self, cfg, tmp_path, monkeypatch):
        self._consent(cfg)
        _, argv = self._fake_cursor_agent(tmp_path, monkeypatch)
        args = ["-p", "--output-format=json", "--", "--output-format=text", "--yolo"]
        result = runner.invoke(app, ["cursor-agent", *args])
        assert result.exit_code == 0, result.output
        assert argv() == args

    def test_wrapper_passes_through_without_consent(self, cfg, tmp_path, monkeypatch):
        from mind_meld import host_usage

        raw, argv = self._fake_cursor_agent(tmp_path, monkeypatch, exit_code=3)
        result = runner.invoke(app, ["cursor-agent", "-p", "--", "-p"])
        assert result.exit_code == 3, result.output
        assert argv() == ["-p", "--", "-p"]
        assert not host_usage.CURSOR_CACHE_PATH.exists()

    def test_wrapper_warns_when_no_result_is_recorded(self, cfg, tmp_path, monkeypatch):
        from mind_meld import host_usage

        self._consent(cfg)
        executable = tmp_path / "cursor-agent"
        executable.write_text("#!/bin/sh\necho 'plain text'\n")
        executable.chmod(0o700)
        monkeypatch.setenv("PATH", str(tmp_path) + ":" + __import__("os").environ["PATH"])
        result = runner.invoke(app, ["cursor-agent", "-p", "hello"])
        assert result.exit_code == 0, result.output
        assert result.stdout == "plain text\n"
        assert "no result line" in result.stderr
        assert not host_usage.CURSOR_CACHE_PATH.exists()

    def test_wrapper_forwards_termination_and_returns_child_status(self, tmp_path):
        import os
        import signal
        import subprocess
        import sys
        import time

        home = tmp_path / "home"
        home.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        ready, got = tmp_path / "ready", tmp_path / "got-term"
        child = bin_dir / "cursor-agent"
        child.write_text(
            f"#!/bin/sh\ntrap 'touch {got}; exit 143' TERM\ntouch {ready}\n"
            "while :; do sleep 0.05; done\n"
        )
        child.chmod(0o700)
        env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        wrapper = subprocess.Popen(
            [sys.executable, "-m", "mind_meld.cli", "cursor-agent", "-p", "hello"], env=env
        )
        try:
            deadline = time.monotonic() + 30
            while not ready.exists():
                assert wrapper.poll() is None and time.monotonic() < deadline
                time.sleep(0.05)
            wrapper.send_signal(signal.SIGTERM)
            assert wrapper.wait(timeout=30) == 143
            assert got.exists()
        finally:
            if wrapper.poll() is None:
                wrapper.kill()

    def test_print_wrapper_preserves_stdout_and_exit_status(self, cfg, tmp_path, monkeypatch):
        import tomllib

        from mind_meld import host_usage

        config = tomllib.loads(cfg.read_text())
        config["retro"] = {"cursor_host_usage": True}
        save_config(config, cfg)
        payload = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "request_id": "wrapper-generation",
            "result": "OK",
            "usage": {
                "inputTokens": 100,
                "cacheWriteTokens": 0,
                "cacheReadTokens": 200,
                "outputTokens": 10,
            },
        }
        executable = tmp_path / "cursor-agent"
        executable.write_text(
            "#!/bin/sh\ncat <<'JSON'\n" + json.dumps(payload) + "\nJSON\nexit 7\n"
        )
        executable.chmod(0o700)
        monkeypatch.setenv("PATH", str(tmp_path) + ":" + __import__("os").environ["PATH"])
        result = runner.invoke(
            app,
            [
                "cursor-agent",
                "--print",
                "--model",
                "grok-4.7-low",
                "--output-format",
                "json",
                "hello",
            ],
        )
        assert result.exit_code == 7, result.output
        assert result.stdout == json.dumps(payload) + "\n"
        history = json.loads(host_usage.CURSOR_CACHE_PATH.read_text())
        assert next(iter(history["runs"].values()))["usage"]["input"] == 100


class TestGrokUsageConsent:
    def test_grok_toggle_patches_source_and_usage_consent_together(
        self, cfg, isolated_seen_sources, monkeypatch
    ):
        """A failed second write must not leave the legacy usage bit enabled."""
        from mind_meld import cli as cli_module

        real_patch = cli_module.patch_config_on_disk
        patches: list[dict] = []

        def capture_patch(updates):
            patches.append(updates)
            real_patch(updates)

        monkeypatch.setattr(cli_module, "patch_config_on_disk", capture_patch)

        enabled = runner.invoke(app, ["enable-source", "grok"])
        assert enabled.exit_code == 0, enabled.output
        assert len(patches) == 1
        assert patches[0]["retro"] == {"grok_host_usage": True}
        assert "sources" in patches[0]["sync"]

        patches.clear()
        disabled = runner.invoke(app, ["disable-source", "grok"])
        assert disabled.exit_code == 0, disabled.output
        assert len(patches) == 1
        assert patches[0]["retro"] == {"grok_host_usage": False}
        assert "grok" in patches[0]["sync"]["disabled_sources"]

    def test_enable_source_grok_appends_source_and_sets_bit(self, cfg, isolated_seen_sources):
        import tomllib

        from mind_meld.config import grok_host_usage_enabled

        result = runner.invoke(app, ["enable-source", "grok"])
        assert result.exit_code == 0, result.output
        assert "Enabled source 'grok'" in result.output
        assert "skills/" in result.output
        assert "not synced" in result.output
        assert "Reads terminal token totals" in result.output

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert on_disk["retro"]["grok_host_usage"] is True
        assert grok_host_usage_enabled(on_disk) is True
        added = next(s for s in on_disk["sync"]["sources"] if s["name"] == "grok")
        assert added == get_default_source("grok")
        assert "grok" not in (on_disk["sync"].get("disabled_sources") or [])

        again = runner.invoke(app, ["enable-source", "grok"])
        assert again.exit_code == 0
        assert "already enabled" in again.output

        off = runner.invoke(app, ["disable-source", "grok"])
        assert off.exit_code == 0, off.output
        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert on_disk["retro"]["grok_host_usage"] is False
        assert "grok" in (on_disk["sync"].get("disabled_sources") or [])

    def test_enable_source_grok_materializes_legacy_sources_before_adding_row(
        self, cfg, isolated_seen_sources, monkeypatch, tmp_path
    ):
        """Explicit Grok enable must not replace a legacy Claude source."""
        import tomllib

        from mind_meld.config import get_sources, load_config, save_config

        home = tmp_path / "home"
        (home / ".grok").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        config = load_config(cfg)
        config["sync"].pop("sources")
        config["sync"]["claude_dir"] = str(tmp_path / ".claude")
        save_config(config, cfg)

        result = runner.invoke(app, ["enable-source", "grok"])
        assert result.exit_code == 0, result.output

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert on_disk["retro"]["grok_host_usage"] is True
        assert next(s for s in on_disk["sync"]["sources"] if s["name"] == "grok") == (
            get_default_source("grok")
        )
        assert [source["name"] for source in get_sources(load_config(cfg))] == ["claude", "grok"]
        assert next(s for s in on_disk["sync"]["sources"] if s["name"] == "claude") == {
            "name": "claude",
            "path": str((tmp_path / ".claude").resolve()),
            "type": "claude",
        }

    def test_enable_source_grok_preserves_existing_retro_keys(self, cfg, isolated_seen_sources):
        import tomllib

        from mind_meld.config import load_config, save_config

        config = load_config(cfg)
        config["retro"] = {"author_emails": ["a@example.com"], "repo_roots": ["~/src"]}
        save_config(config, cfg)

        result = runner.invoke(app, ["enable-source", "grok"])
        assert result.exit_code == 0, result.output
        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert on_disk["retro"]["author_emails"] == ["a@example.com"]
        assert on_disk["retro"]["repo_roots"] == ["~/src"]
        assert on_disk["retro"]["grok_host_usage"] is True

    def test_enable_source_grok_scrubs_leftover_disabled_sources(self, cfg, isolated_seen_sources):
        import tomllib

        from mind_meld.config import load_config, save_config

        config = load_config(cfg)
        config["sync"]["disabled_sources"] = ["gstack", "grok"]
        save_config(config, cfg)

        result = runner.invoke(app, ["enable-source", "grok"])
        assert result.exit_code == 0, result.output
        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert on_disk["retro"]["grok_host_usage"] is True
        assert on_disk["sync"]["disabled_sources"] == ["gstack"]

    def test_disable_source_grok_does_not_clear_other_retro_keys(self, cfg, isolated_seen_sources):
        import tomllib

        from mind_meld.config import load_config, save_config

        config = load_config(cfg)
        config["retro"] = {"author_emails": ["a@example.com"], "grok_host_usage": True}
        save_config(config, cfg)
        runner.invoke(app, ["enable-source", "grok"])
        off = runner.invoke(app, ["disable-source", "grok"])
        assert off.exit_code == 0, off.output
        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert on_disk["retro"]["author_emails"] == ["a@example.com"]
        assert on_disk["retro"]["grok_host_usage"] is False

    def test_grok_is_a_known_source_name(self):
        _validate_source_name("grok", {"sync": {"sources": []}}, force=False)
        assert "grok" in _known_source_names({})


class TestReconfigureSources:
    def test_bare_grok_root_defaults_off(self, cfg, isolated_seen_sources, monkeypatch, tmp_path):
        """Reconfigure must not turn a stock ~/.grok into source or consent."""
        import tomllib

        from mind_meld.config import load_config

        home = tmp_path / "home"
        claude = home / ".claude"
        grok = home / ".grok"
        claude.mkdir(parents=True)
        grok.mkdir()
        monkeypatch.setenv("HOME", str(home))

        config = load_config(cfg)
        config["sync"] = {
            "claude_dir": str(claude),
            "max_file_size": 52_428_800,
        }
        save_config(config, cfg)

        grok_prompt: dict[str, object] = {}

        def accept_default(prompt, default):
            if "'grok'" in prompt:
                grok_prompt.update(prompt=prompt, default=default)
            return default

        monkeypatch.setattr(typer, "confirm", accept_default)
        result = runner.invoke(app, ["reconfigure-sources"])
        assert result.exit_code == 0, result.output
        assert grok_prompt == {
            "prompt": "Sync 'grok' source at ~/.grok? (not detected)",
            "default": False,
        }

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert "grok" not in [s["name"] for s in on_disk["sync"]["sources"]]
        assert on_disk["retro"]["grok_host_usage"] is False

    def test_usage_only_grok_opt_in_defaults_on(
        self, cfg, isolated_seen_sources, monkeypatch, tmp_path
    ):
        """A 21A Grok usage opt-in remains consented through reconfigure."""
        import tomllib

        from mind_meld.config import load_config

        home = tmp_path / "home"
        (home / ".grok").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))

        config = load_config(cfg)
        config["sync"]["sources"] = []
        config["retro"] = {"grok_host_usage": True}
        save_config(config, cfg)

        grok_prompt: dict[str, object] = {}

        def accept_default(prompt, default):
            if "'grok'" in prompt:
                grok_prompt.update(prompt=prompt, default=default)
            return default

        monkeypatch.setattr(typer, "confirm", accept_default)
        result = runner.invoke(app, ["reconfigure-sources"])
        assert result.exit_code == 0, result.output
        assert grok_prompt == {
            "prompt": "Sync 'grok' source at ~/.grok? (not detected)",
            "default": True,
        }

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert on_disk["retro"]["grok_host_usage"] is True
        assert next(s for s in on_disk["sync"]["sources"] if s["name"] == "grok") == (
            get_default_source("grok")
        )

    def test_explicit_empty_sources_defaults_non_grok_off(
        self, cfg, isolated_seen_sources, monkeypatch
    ):
        """An explicit empty sources list means nothing is currently active —
        not "fall back to whether this name is a default". Sibling regression
        to test_enable_default_from_explicit_empty_sources
        (tests/test_integration.py): enable-source's has_explicit_sources fix
        did not originally extend to reconfigure_sources, which still used
        bare `not explicit_sources` list-truthiness and defaulted every
        DEFAULT_SOURCES prompt to Y even when the user had explicitly zeroed
        the list out."""
        from mind_meld.config import load_config

        config = load_config(cfg)
        config["sync"]["sources"] = []
        save_config(config, cfg)

        claude_prompt: dict[str, object] = {}

        def accept_default(prompt, default):
            if "'claude'" in prompt:
                claude_prompt.update(prompt=prompt, default=default)
            return default

        monkeypatch.setattr(typer, "confirm", accept_default)
        result = runner.invoke(app, ["reconfigure-sources"])
        assert result.exit_code == 0, result.output
        assert claude_prompt["default"] is False


# ── mm sources display ────────────────────────────────────────────────


class TestSourcesTable:
    def test_shows_enabled_column(self, cfg, isolated_seen_sources):
        result = runner.invoke(app, ["sources"])
        assert result.exit_code == 0, result.output
        assert "Enabled" in result.output

    def test_shows_disabled_row_too(self, cfg, isolated_seen_sources):
        runner.invoke(app, ["disable-source", "gstack"])
        result = runner.invoke(app, ["sources"])
        assert result.exit_code == 0
        # Both sources visible in the table even with one disabled.
        assert "claude" in result.output
        assert "gstack" in result.output


# ── mm status display ─────────────────────────────────────────────────


class TestStatusBreadcrumbs:
    def test_disabled_breadcrumb_when_nonempty(self, cfg, isolated_seen_sources, monkeypatch):
        """`mm status` shows disabled-sources line when list is non-empty."""
        from mind_meld import crypto

        monkeypatch.setenv("MINDMELD_PASSPHRASE", "test-passphrase")

        # Bootstrap crypto-init in storage so status can run.
        import tomllib

        from mind_meld.crypto import bootstrap_crypto_init
        from mind_meld.devices import register_device
        from mind_meld.storage.local import LocalBackend

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        backend = LocalBackend(on_disk["storage"]["path"])
        bootstrap_crypto_init(backend, "test-passphrase", argon2_memory_kb=1024)
        register_device(backend, "abc123", "MacBook")
        crypto.clear_crypto_session()

        runner.invoke(app, ["disable-source", "gstack"])
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "Disabled sources" in result.output
        assert "gstack" in result.output

    def test_no_breadcrumb_when_empty(self, cfg, isolated_seen_sources, monkeypatch):
        from mind_meld import crypto
        from mind_meld.crypto import bootstrap_crypto_init
        from mind_meld.devices import register_device
        from mind_meld.storage.local import LocalBackend

        monkeypatch.setenv("MINDMELD_PASSPHRASE", "test-passphrase")

        import tomllib

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        backend = LocalBackend(on_disk["storage"]["path"])
        bootstrap_crypto_init(backend, "test-passphrase", argon2_memory_kb=1024)
        register_device(backend, "abc123", "MacBook")
        crypto.clear_crypto_session()

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "Disabled sources" not in result.output
        assert "Grok usage capture" not in result.output

    def test_status_omits_disabled_names_with_no_resolvable_source(
        self, cfg, isolated_seen_sources, monkeypatch
    ):
        """Pre-existing: `mm disable-source foo --force` used to print a
        re-enable breadcrumb for a name that cannot resolve. Intersection
        with `_resolve_all_configured_sources` drops those names."""
        from mind_meld import crypto
        from mind_meld.crypto import bootstrap_crypto_init
        from mind_meld.devices import register_device
        from mind_meld.storage.local import LocalBackend

        monkeypatch.setenv("MINDMELD_PASSPHRASE", "test-passphrase")

        import tomllib

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        backend = LocalBackend(on_disk["storage"]["path"])
        bootstrap_crypto_init(backend, "test-passphrase", argon2_memory_kb=1024)
        register_device(backend, "abc123", "MacBook")
        crypto.clear_crypto_session()

        forced = runner.invoke(app, ["disable-source", "not-a-real-source", "--force"])
        assert forced.exit_code == 0, forced.output
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "Disabled sources" not in result.output
        assert "not-a-real-source" not in result.output

    def test_grok_status_when_enabled_or_present(self, cfg, isolated_seen_sources, monkeypatch):
        from mind_meld import crypto
        from mind_meld import host_usage as _host_usage
        from mind_meld.crypto import bootstrap_crypto_init
        from mind_meld.devices import register_device
        from mind_meld.storage.local import LocalBackend

        monkeypatch.setenv("MINDMELD_PASSPHRASE", "test-passphrase")

        import tomllib

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        backend = LocalBackend(on_disk["storage"]["path"])
        bootstrap_crypto_init(backend, "test-passphrase", argon2_memory_kb=1024)
        register_device(backend, "abc123", "MacBook")
        crypto.clear_crypto_session()

        enabled = runner.invoke(app, ["enable-source", "grok"])
        assert enabled.exit_code == 0, enabled.output
        shown = runner.invoke(app, ["status"])
        assert shown.exit_code == 0, shown.output
        assert "Grok usage capture: enabled, but no successful scan yet" in shown.output

        _host_usage.GROK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _host_usage.GROK_CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": _host_usage.CACHE_VERSION,
                    "complete_once": True,
                    "usage_less_skipped": 0,
                    "files": {},
                }
            ),
            encoding="utf-8",
        )
        historical = runner.invoke(app, ["status"])
        assert historical.exit_code == 0, historical.output
        assert (
            "Grok usage capture: enabled; a prior scan completed successfully" in historical.output
        )
        assert "last scan complete" not in historical.output
        assert "Grok usage capture: enabled, publishing" not in historical.output

        _host_usage.GROK_CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": _host_usage.CACHE_VERSION,
                    "complete_once": False,
                    "usage_less_skipped": 0,
                    "last_reason": "unsupported",
                    "files": {},
                }
            ),
            encoding="utf-8",
        )
        drifted = runner.invoke(app, ["status"])
        assert drifted.exit_code == 0, drifted.output
        # Publication prerequisites come first. This fixture has never selected
        # mm-events; only after enabling it can reader recovery be actionable.
        assert "mm enable-source mm-events" in " ".join(drifted.output.split())
        assert "mm push" not in " ".join(drifted.output.split())
        enabled = runner.invoke(app, ["enable-source", "mm-events"])
        assert enabled.exit_code == 0, enabled.output
        drifted = runner.invoke(app, ["status"])
        assert drifted.exit_code == 0, drifted.output
        assert "pipx upgrade mind-meld" in drifted.output
        assert "disable-source" in drifted.output
        assert "no successful scan yet — run" not in drifted.output

        _host_usage.GROK_CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": _host_usage.CACHE_VERSION,
                    "complete_once": True,
                    "usage_less_skipped": 0,
                    "last_reason": "unsupported",
                    "files": {},
                }
            ),
            encoding="utf-8",
        )
        latched = runner.invoke(app, ["status"])
        assert latched.exit_code == 0, latched.output
        assert "pipx upgrade mind-meld" in latched.output
        assert "prior scan completed successfully" not in latched.output

        _host_usage.GROK_CACHE_PATH.write_text(
            json.dumps(
                {
                    "version": _host_usage.CACHE_VERSION,
                    "complete_once": False,
                    "usage_less_skipped": 0,
                    "last_reason": "deadline",
                    "files": {},
                }
            ),
            encoding="utf-8",
        )
        warming = runner.invoke(app, ["status"])
        assert warming.exit_code == 0, warming.output
        warming_text = " ".join(warming.output.split())
        assert "(grok deadline)" in warming_text
        assert "no successful scan yet" not in warming_text
        assert "Last read allowed unknown ms; last complete read unknown ms" in warming_text

        runner.invoke(app, ["disable-source", "grok"])
        _host_usage.GROK_SESSIONS_PATH.mkdir(parents=True)
        present = runner.invoke(app, ["status"])
        assert present.exit_code == 0, present.output
        assert "Grok usage capture: disabled" in present.output
        assert "mm enable-source grok" in present.output

    def test_grok_source_without_legacy_bit_reports_enabled(
        self, cfg, isolated_seen_sources, tmp_path, monkeypatch
    ):
        """The source gate itself authorizes capture after Track 22B."""
        from mind_meld import crypto
        from mind_meld.config import load_config
        from mind_meld.crypto import bootstrap_crypto_init
        from mind_meld.devices import register_device
        from mind_meld.storage.local import LocalBackend

        grok = tmp_path / ".grok"
        grok.mkdir()
        config = load_config(cfg)
        config["sync"]["sources"].append({"name": "grok", "path": str(grok), "type": "grok"})
        save_config(config, cfg)
        monkeypatch.setenv("MINDMELD_PASSPHRASE", "test-passphrase")

        backend = LocalBackend(config["storage"]["path"])
        bootstrap_crypto_init(backend, "test-passphrase", argon2_memory_kb=1024)
        register_device(backend, "abc123", "MacBook")
        crypto.clear_crypto_session()

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "Grok usage capture: enabled, but no successful scan yet" in result.output


# ── _filter_disabled_sources ─────────────────────────────────────────


class TestFilterDisabledSources:
    """Pure-function tests for the manifest filter (consumer-boundary
    invariant). End-to-end tombstone-suppression behavior pinned in
    test_integration.py::TestDisabledSourcesTombstoneSuppression."""

    def test_empty_disabled_returns_unchanged(self):
        manifest = {
            "sources": {"claude": {"files": {"a.md": {"sha256": "x"}}}},
            "tombstones": {"claude:b.md": {"deleted_by": "x"}},
        }
        assert _filter_disabled_sources(manifest, []) is manifest

    def test_drops_disabled_source_entries(self):
        manifest = {
            "sources": {
                "claude": {"files": {"a.md": {"sha256": "x"}}},
                "gstack": {"files": {"b.md": {"sha256": "y"}}},
            },
            "tombstones": {},
        }
        out = _filter_disabled_sources(manifest, ["gstack"])
        assert "claude" in out["sources"]
        assert "gstack" not in out["sources"]

    def test_preserves_tombstones_for_disabled_source(self):
        """Asymmetric filter: tombstones survive across the disable filter
        even for the disabled source. Pinning the codex 2026-04-25 finding:
        if A deleted gstack:x and pushed a tombstone, then disables gstack,
        the prior tombstone MUST flow through to A's next manifest so a
        long-offline peer pulling only A's view doesn't lose the deletion."""
        manifest = {
            "sources": {},
            "tombstones": {
                "claude:a.md": {"deleted_by": "x"},
                "gstack:b.md": {"deleted_by": "x"},
            },
        }
        out = _filter_disabled_sources(manifest, ["gstack"])
        # BOTH tombstones flow through — disable does not undo prior consensus.
        assert "claude:a.md" in out["tombstones"]
        assert "gstack:b.md" in out["tombstones"]

    def test_unselected_source_drops_files_preserves_tombstones(self):
        manifest = {
            "sources": {
                "claude": {"files": {"a.md": {"sha256": "x"}}},
                "retired": {"files": {"old.md": {"sha256": "y"}}},
            },
            "tombstones": {
                "claude:gone.md": {"deleted_at": "2026-01-01T00:00:00+00:00"},
                "retired:old.md": {"deleted_at": "2026-01-01T00:00:00+00:00"},
            },
        }
        out = _filter_unselected_sources(manifest, {"claude"})
        assert "retired" not in out["sources"]
        assert "claude" in out["sources"]
        assert "retired:old.md" in out["tombstones"]
        assert "claude:gone.md" in out["tombstones"]
