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
    app,
)
from mind_meld.config import get_default_source, save_config
from mind_meld.core import _prompt_source_toggle
from mind_meld.errors import ConfigError
from mind_meld.sourceflow import _known_source_names, _validate_source_name

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

    def test_enable_source_grok_prints_memory_scope_and_opt_in_note_on_first_and_repeat_enable(
        self, cfg, isolated_seen_sources
    ):
        first = runner.invoke(app, ["enable-source", "grok"])
        assert first.exit_code == 0, first.output
        first_norm = " ".join(first.output.split())
        assert "Enabled source 'grok' on this device." in first_norm
        assert "Observations, sessions, SQLite indexes, MEMORY.md" in first_norm
        assert "Syncs ~/.grok skills/, commands/, rules/, and memory-v2/ topics." in first_norm
        assert (
            "Grok keeps memory off by default; turn it on on each machine with "
            "[memory] enabled = true in ~/.grok/config.toml (or GROK_MEMORY=1)." in first_norm
        )

        repeat = runner.invoke(app, ["enable-source", "grok"])
        assert repeat.exit_code == 0, repeat.output
        repeat_norm = " ".join(repeat.output.split())
        assert "Source 'grok' is already enabled." in repeat_norm
        assert "Observations, sessions, SQLite indexes, MEMORY.md" in repeat_norm
        assert "Syncs ~/.grok skills/, commands/, rules/, and memory-v2/ topics." in repeat_norm
        assert (
            "Grok keeps memory off by default; turn it on on each machine with "
            "[memory] enabled = true in ~/.grok/config.toml (or GROK_MEMORY=1)." in repeat_norm
        )

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
        assert "source 'opencode' was retired" in combined
        assert "mm migrate-config" in combined
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

    @pytest.mark.parametrize("source_name", ["codex", "grok", "muse"])
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


class TestCursorRemoved114A:
    @pytest.mark.parametrize(
        "argv,stdin",
        [
            (["cursor-agent", "-p", "hello"], None),
            (["capture-cursor-usage"], "{}"),
        ],
    )
    def test_removed_cursor_commands_exit_2_without_spool_writes(self, cfg, argv, stdin):
        from mind_meld import host_usage

        result = runner.invoke(app, argv, input=stdin)
        assert result.exit_code == 2
        assert not host_usage.CURSOR_SPOOL_PATH.exists()

    @pytest.mark.parametrize("command", ["enable-source", "disable-source"])
    def test_cursor_is_an_unknown_source_name(self, cfg, isolated_seen_sources, command):
        from mind_meld import host_usage

        assert "cursor" not in _known_source_names({})
        before = cfg.read_bytes()
        result = runner.invoke(app, [command, "cursor"])
        assert result.exit_code == 1
        assert "unknown source 'cursor'" in result.output
        assert cfg.read_bytes() == before
        assert not host_usage.CURSOR_HOOK_CONFIG_PATH.exists()

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
        assert "retro" not in after


class TestRetiredSourceToggle:
    @pytest.mark.parametrize("keep_claude", [False, True])
    def test_reconfigure_records_retired_sources_without_inspecting_their_fields(
        self, cfg, isolated_seen_sources, monkeypatch, keep_claude
    ):
        from mind_meld.config import load_config

        config = load_config(cfg)
        config["sync"]["sources"] = [
            *([config["sync"]["sources"][0]] if keep_claude else []),
            {"name": "opencode", "path": 17, "exclude_patterns": False},
        ]
        save_config(config, cfg)
        prompts = []

        def choose(prompt, default):
            prompts.append(prompt)
            return keep_claude and "'claude'" in prompt

        monkeypatch.setattr(typer, "confirm", choose)
        result = runner.invoke(app, ["reconfigure-sources"])
        assert result.exit_code == 0, result.output
        on_disk = load_config(cfg)
        assert [s["name"] for s in on_disk["sync"]["sources"]] == (
            ["claude"] if keep_claude else []
        )
        assert {"opencode"} <= set(on_disk["sync"]["disabled_sources"])
        assert not any("opencode" in p for p in prompts)

    @pytest.mark.parametrize("name", ["opencode"])
    @pytest.mark.parametrize("force", [False, True])
    def test_enable_source_retired_name_rejected_even_with_force(self, cfg, name, force):
        cmd = ["enable-source", name]
        if force:
            cmd.append("--force")
        result = runner.invoke(app, cmd)
        assert result.exit_code != 0
        assert f"source '{name}' was retired" in result.output
        assert "mm migrate-config" in result.output

    @pytest.mark.parametrize("name", ["opencode"])
    def test_retired_sources_excluded_from_known_source_names(self, name):
        assert name not in _known_source_names({})
        assert name not in _known_source_names(
            {"sync": {"sources": [{"name": name, "path": f"~/.{name}", "type": name}]}}
        )


class TestGrokSourceToggle:
    def test_enable_source_grok_from_v1118_disabled_sources_state(self, cfg, isolated_seen_sources):
        """Enabling grok after v1.11.8 migrate-config removes it from disabled_sources
        and appends the default grok source."""
        import tomllib

        from mind_meld.config import grok_usage_consented, load_config

        with open(cfg, "rb") as f:
            raw = tomllib.load(f)
        raw["sync"]["disabled_sources"] = ["grok", "gstack"]
        save_config(raw, cfg)
        assert grok_usage_consented(load_config(cfg)) is False

        result = runner.invoke(app, ["enable-source", "grok"])
        assert result.exit_code == 0, result.output
        assert "Enabled source 'grok'" in result.output
        assert "skills/, commands/, rules/, and memory-v2/" in result.output
        assert "mm disable-source grok" in result.output

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert on_disk["sync"].get("disabled_sources") == ["gstack"]
        added = next(s for s in on_disk["sync"]["sources"] if s["name"] == "grok")
        assert added == get_default_source("grok")
        assert grok_usage_consented(load_config(cfg)) is True

        off = runner.invoke(app, ["disable-source", "grok"])
        assert off.exit_code == 0, off.output
        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert "grok" in (on_disk["sync"].get("disabled_sources") or [])
        assert any(s["name"] == "grok" for s in on_disk["sync"]["sources"])
        assert grok_usage_consented(load_config(cfg)) is False


class TestMuseSourceToggle:
    def test_enable_source_muse_appends_source_without_retro_bit(self, cfg, isolated_seen_sources):
        """Muse consent is source-enabled only; no [retro] key is written."""
        import tomllib

        result = runner.invoke(app, ["enable-source", "muse"])
        assert result.exit_code == 0, result.output
        assert "Enabled source 'muse'" in result.output
        assert "memory/" in result.output
        assert "not synced" in result.output
        assert "model-step" in result.output

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert "muse_host_usage" not in (on_disk.get("retro") or {})
        added = next(s for s in on_disk["sync"]["sources"] if s["name"] == "muse")
        assert added == get_default_source("muse")
        assert "muse" not in (on_disk["sync"].get("disabled_sources") or [])

        again = runner.invoke(app, ["enable-source", "muse"])
        assert again.exit_code == 0
        assert "already enabled" in again.output

        off = runner.invoke(app, ["disable-source", "muse"])
        assert off.exit_code == 0, off.output
        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert "muse" in (on_disk["sync"].get("disabled_sources") or [])

    @pytest.mark.parametrize("name", ["grok", "muse", "agy"])
    def test_enable_source_materializes_legacy_sources_before_adding_row(
        self, cfg, isolated_seen_sources, monkeypatch, tmp_path, name
    ):
        """Explicit host enable must preserve legacy Claude and record consent."""
        import tomllib

        from mind_meld.config import get_sources, load_config, save_config

        home = tmp_path / "home"
        (home / ".grok" / "skills").mkdir(parents=True)
        (home / ".local" / "share" / "muse").mkdir(parents=True)
        (home / ".gemini" / "antigravity-cli" / "conversations").mkdir(parents=True)
        monkeypatch.setattr(
            "mind_meld.host_usage.AGY_CONVERSATIONS_PATH",
            home / ".gemini/antigravity-cli/conversations",
        )
        monkeypatch.setenv("HOME", str(home))
        config = load_config(cfg)
        config["sync"].pop("sources")
        config["sync"]["claude_dir"] = str(tmp_path / ".claude")
        save_config(config, cfg)

        result = runner.invoke(app, ["enable-source", name])
        assert result.exit_code == 0, result.output
        assert f"Enabled source '{name}'" in result.output
        if name == "agy":
            assert "skills/" in result.output
            assert "rules/" in result.output
            assert "local Antigravity" in result.output
            assert "not synced" in result.output
            assert "mm disable-source agy" in result.output

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert next(s for s in on_disk["sync"]["sources"] if s["name"] == name) == (
            get_default_source(name)
        )
        assert [source["name"] for source in get_sources(load_config(cfg))] == ["claude", name]
        assert next(s for s in on_disk["sync"]["sources"] if s["name"] == "claude") == {
            "name": "claude",
            "path": str((tmp_path / ".claude").resolve()),
            "type": "claude",
        }

    def test_muse_is_a_known_source_name(self):
        _validate_source_name("muse", {"sync": {"sources": []}}, force=False)
        assert "muse" in _known_source_names({})


class TestReconfigureSources:
    @pytest.mark.parametrize(
        "detected_tree", [None, ".gemini/config", ".gemini/antigravity-cli/conversations"]
    )
    def test_legacy_agy_prompt_defaults_to_detection(
        self, cfg, isolated_seen_sources, monkeypatch, tmp_path, detected_tree
    ):
        """Interactive enrollment offers the detected host, never an absent one."""
        from mind_meld.config import load_config

        home = tmp_path / "home"
        home.mkdir()
        if detected_tree:
            (home / detected_tree).mkdir(parents=True)
        monkeypatch.setattr(
            "mind_meld.host_usage.AGY_CONVERSATIONS_PATH",
            home / ".gemini/antigravity-cli/conversations",
        )
        monkeypatch.setenv("HOME", str(home))
        config = load_config(cfg)
        config["sync"].pop("sources")
        config["sync"]["claude_dir"] = str(tmp_path / ".claude")
        save_config(config, cfg)
        defaults = []

        def accept_default(prompt, default):
            if "'agy'" in prompt:
                defaults.append(default)
            return default

        monkeypatch.setattr(typer, "confirm", accept_default)
        result = runner.invoke(app, ["reconfigure-sources"])
        assert result.exit_code == 0, result.output
        assert defaults == [detected_tree is not None]
        on_disk = load_config(cfg)
        assert ("agy" in [s["name"] for s in on_disk["sync"]["sources"]]) is (
            detected_tree is not None
        )

    def test_reconfigure_sources_skips_retired_sources(
        self, cfg, isolated_seen_sources, monkeypatch
    ):
        """Reconfigure never prompts for retired opencode, and prompts grok with default False."""
        prompts: list[tuple[str, bool]] = []

        def accept(prompt, default):
            prompts.append((prompt, default))
            return default

        monkeypatch.setattr(typer, "confirm", accept)
        result = runner.invoke(app, ["reconfigure-sources"])
        assert result.exit_code == 0
        assert not any("opencode" in p for p, _ in prompts)
        grok_prompts = [(p, d) for p, d in prompts if "'grok'" in p]
        assert len(grok_prompts) == 1
        assert grok_prompts[0][1] is False

    @pytest.mark.parametrize("detected_subdir", [None, "skills"])
    def test_grok_defaults_off_even_when_detected(
        self, cfg, isolated_seen_sources, monkeypatch, tmp_path, detected_subdir
    ):
        """Reconfigure must not turn a detected ~/.grok tree into source or consent without Y."""
        import tomllib

        from mind_meld.config import load_config

        home = tmp_path / "home"
        claude = home / ".claude"
        claude.mkdir(parents=True)
        (home / ".grok" / "sessions").mkdir(parents=True)
        (home / ".grok" / "auth.json").write_text("{}\n")
        if detected_subdir:
            (home / ".grok" / detected_subdir).mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        assert "grok" in _known_source_names({})

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
        expected_suffix = "(detected)" if detected_subdir else "(not detected)"
        assert grok_prompt == {
            "prompt": f"Sync 'grok' source at ~/.grok? {expected_suffix}",
            "default": False,
        }

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert "grok" not in [s["name"] for s in on_disk["sync"]["sources"]]

    def test_bare_muse_root_defaults_off(self, cfg, isolated_seen_sources, monkeypatch, tmp_path):
        """Reconfigure must not turn a stock Muse data root into source or consent."""
        import tomllib

        from mind_meld.config import load_config

        home = tmp_path / "home"
        claude = home / ".claude"
        claude.mkdir(parents=True)
        (home / ".local" / "share" / "muse" / "sessions").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))

        config = load_config(cfg)
        config["sync"] = {
            "claude_dir": str(claude),
            "max_file_size": 52_428_800,
        }
        save_config(config, cfg)

        muse_prompt: dict[str, object] = {}

        def accept_default(prompt, default):
            if "'muse'" in prompt:
                muse_prompt.update(prompt=prompt, default=default)
            return default

        monkeypatch.setattr(typer, "confirm", accept_default)
        result = runner.invoke(app, ["reconfigure-sources"])
        assert result.exit_code == 0, result.output
        assert muse_prompt == {
            "prompt": "Sync 'muse' source at ~/.local/share/muse? (not detected)",
            "default": False,
        }

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        assert "muse" not in [s["name"] for s in on_disk["sync"]["sources"]]

    def test_explicit_empty_sources_defaults_all_off(self, cfg, isolated_seen_sources, monkeypatch):
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

    @pytest.mark.parametrize("block_kept", [True, False])
    def test_reconfigure_reenrolls_disabled_grok(
        self, cfg, isolated_seen_sources, monkeypatch, block_kept
    ):
        """Accepting Grok in reconfigure-sources after it was disabled restores
        the canonical block and consent, removes grok from disabled_sources and
        keeps other disabled names."""
        import tomllib

        from mind_meld.config import grok_usage_consented, load_config

        with open(cfg, "rb") as f:
            raw = tomllib.load(f)
        if block_kept:
            raw["sync"]["sources"].append(get_default_source("grok"))
        raw["sync"]["disabled_sources"] = ["grok", "gstack"]
        save_config(raw, cfg)
        assert grok_usage_consented(load_config(cfg)) is False

        monkeypatch.setattr(
            typer,
            "confirm",
            lambda prompt, default: True if "'grok'" in prompt else default,
        )
        result = runner.invoke(app, ["reconfigure-sources"])
        assert result.exit_code == 0, result.output

        with open(cfg, "rb") as f:
            on_disk = tomllib.load(f)
        grok_entries = [s for s in on_disk["sync"]["sources"] if s["name"] == "grok"]
        assert grok_entries == [get_default_source("grok")]
        disabled_after = on_disk["sync"].get("disabled_sources") or []
        assert "grok" not in disabled_after
        assert "gstack" in disabled_after
        assert grok_usage_consented(load_config(cfg)) is True


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

    def test_muse_status_nudge_enabled_and_disabled(
        self, cfg, isolated_seen_sources, monkeypatch, tmp_path
    ):
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
        home = tmp_path / "home"
        sessions = home / ".local" / "share" / "muse" / "sessions"
        sessions.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(_host_usage, "MUSE_SESSIONS_PATH", sessions)

        nudged = runner.invoke(app, ["status"])
        assert nudged.exit_code == 0, nudged.output
        flat = " ".join(nudged.output.split())
        assert "Muse usage capture: disabled" in flat
        assert "mm enable-source muse" in flat

        assert runner.invoke(app, ["enable-source", "muse"]).exit_code == 0
        enabled = runner.invoke(app, ["status"])
        assert enabled.exit_code == 0, enabled.output
        assert "Muse usage capture: enabled, but no successful scan yet" in enabled.output

        assert runner.invoke(app, ["disable-source", "muse"]).exit_code == 0
        probes: list[object] = []
        real_root = _host_usage.muse_sessions_root
        monkeypatch.setattr(
            _host_usage, "muse_sessions_root", lambda: probes.append(real_root()) or real_root()
        )
        silent = runner.invoke(app, ["status"])
        assert silent.exit_code == 0, silent.output
        # Explicitly disabled: no presence probe and no nudge.
        assert "Muse usage capture" not in silent.output
        assert probes == []


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


@pytest.mark.parametrize(
    "command,expected",
    [
        ("sources", []),
        ("disable-source", [("name", ["name"]), ("force", ["--force"])]),
        ("enable-source", [("name", ["name"]), ("force", ["--force"])]),
        ("reconfigure-sources", []),
    ],
)
def test_source_command_shells_keep_their_parameters(command, expected):
    """The bodies moved to sourceflow, whose contract test cannot import cli."""
    import typer.main

    params = typer.main.get_command(app).commands[command].params
    assert [(p.name, p.opts) for p in params] == expected
