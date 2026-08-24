"""Tests for mind_meld.config — TOML load/save, validation."""

import os
from pathlib import Path

import pytest

from mind_meld.config import (
    DEFAULT_SOURCES,
    _apply_defaults,
    _validate,
    _validate_exclude_patterns,
    _validate_sources,
    get_sources,
    grok_host_usage_enabled,
    load_config,
    patch_config_on_disk,
    save_config,
)
from mind_meld.errors import ConfigError


class TestValidation:
    def test_valid_config(self):
        config = {
            "device": {"id": "abc123", "name": "MacBook"},
            "storage": {"path": "/tmp/mind-meld"},
        }
        _validate(config)  # should not raise

    def test_missing_device_section(self):
        config = {"storage": {"path": "/tmp"}}
        with pytest.raises(ConfigError, match="missing.*device"):
            _validate(config)

    def test_missing_device_id(self):
        config = {
            "device": {"name": "MacBook"},
            "storage": {"path": "/tmp"},
        }
        with pytest.raises(ConfigError, match="missing.*device.id"):
            _validate(config)

    def test_missing_storage_section(self):
        config = {"device": {"id": "abc", "name": "MacBook"}}
        with pytest.raises(ConfigError, match="missing.*storage"):
            _validate(config)

    def test_missing_storage_path(self):
        config = {
            "device": {"id": "abc", "name": "MacBook"},
            "storage": {},
        }
        with pytest.raises(ConfigError, match="missing.*storage.path"):
            _validate(config)


class TestDefaults:
    def test_applies_sync_defaults(self):
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": "/tmp/mind-meld"},
        }
        _apply_defaults(config)
        assert config["sync"]["max_file_size"] == 52_428_800
        assert config["crypto"]["argon2_memory_kb"] == 65_536

    def test_does_not_overwrite_existing(self):
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": "/tmp/mind-meld"},
            "sync": {"claude_dir": "/custom/claude", "max_file_size": 10_000_000},
            "crypto": {"argon2_memory_kb": 32_768},
        }
        _apply_defaults(config)
        assert config["sync"]["max_file_size"] == 10_000_000
        assert config["crypto"]["argon2_memory_kb"] == 32_768


class TestSaveLoad:
    def test_round_trip(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config = {
            "device": {"id": "abc123", "name": "MacBook Pro"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {"claude_dir": str(tmp_path / ".claude"), "max_file_size": 52_428_800},
            "crypto": {"argon2_memory_kb": 65_536},
        }
        save_config(config, config_path)
        loaded = load_config(config_path)
        assert loaded["device"]["id"] == "abc123"
        assert loaded["device"]["name"] == "MacBook Pro"
        assert loaded["storage"]["path"]

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="config not found"):
            load_config(tmp_path / "nonexistent.toml")

    def test_save_routes_through_fsutil_with_fsync_true(self, tmp_path, monkeypatch):
        """save_config must use fsutil.atomic_write_bytes with fsync=True —
        a corrupt config locks the user out of running mm entirely."""
        from mind_meld import config as config_module

        calls: list[dict] = []
        real_write = config_module.fsutil.atomic_write_bytes

        def spy_write(path, data, *, fsync=False, mode=None):
            calls.append({"path": path, "fsync": fsync, "mode": mode})
            real_write(path, data, fsync=fsync, mode=mode)

        monkeypatch.setattr(config_module.fsutil, "atomic_write_bytes", spy_write)
        config_path = tmp_path / "config.toml"
        config = {
            "device": {"id": "x", "name": "n"},
            "storage": {"path": str(tmp_path / "s")},
            "sync": {"claude_dir": str(tmp_path / ".claude"), "max_file_size": 1},
            "crypto": {"argon2_memory_kb": 1},
        }
        save_config(config, config_path)
        assert len(calls) == 1
        assert calls[0]["fsync"] is True


class TestDefaultSources:
    """Pin DEFAULT_SOURCES contents so accidental list trims become test failures.

    Specifically guards the gstack source's include_files curation (gstack
    config + cross-machine memory + onboarding markers). Adding here widens
    the default sync surface for all new mm installs, so the contents are
    deliberately enumerated.
    """

    def test_claude_source_present(self):
        assert any(s["name"] == "claude" and s["type"] == "claude" for s in DEFAULT_SOURCES)

    def test_gstack_source_present(self):
        assert any(s["name"] == "gstack" and s["type"] == "generic" for s in DEFAULT_SOURCES)

    def test_gstack_include_dirs_unchanged(self):
        gstack = next(s for s in DEFAULT_SOURCES if s["name"] == "gstack")
        assert gstack["include_dirs"] == ["projects", "analytics", "retros"]

    def test_gstack_include_files_contains_memory_content(self):
        """Memory content (retro-context.md, greptile-history.md) must be in the
        default include_files. Added 2026-04-24 for cross-machine memory continuity."""
        gstack = next(s for s in DEFAULT_SOURCES if s["name"] == "gstack")
        assert "retro-context.md" in gstack["include_files"]
        assert "greptile-history.md" in gstack["include_files"]

    def test_gstack_include_files_contains_onboarding_markers(self):
        """Onboarding markers determine which gstack first-run prompts have already
        been seen. Losing them silently re-prompts users on every machine."""
        gstack = next(s for s in DEFAULT_SOURCES if s["name"] == "gstack")
        for marker in (
            ".completeness-intro-seen",
            ".telemetry-prompted",
            ".proactive-prompted",
            ".welcome-seen",
            ".codex-desc-healed",
        ):
            assert marker in gstack["include_files"], f"missing onboarding marker {marker}"

    def test_gstack_include_files_no_longer_contains_config(self):
        """v0.9.3: config.yaml moved from include_files to exclude_patterns.
        It holds gstack version-check tracking (last successful version per
        machine), so syncing it actively breaks the version mechanism on
        whichever machine pulls last."""
        gstack = next(s for s in DEFAULT_SOURCES if s["name"] == "gstack")
        assert "config.yaml" not in gstack["include_files"]

    def test_gstack_exclude_patterns_present(self):
        """5C: per-machine artifacts (repo-mode.json, land-deploy-confirmed)
        must be in the default exclude_patterns. Losing this regression-pins
        the 2026-04-24 first-pull conflict bug. v0.9.3 added
        config.yaml for the gstack version-check reason (see test above).
        v0.11.13 added analytics/.last-sync-* (per-machine cursor files
        that churn-conflict on every pull)."""
        gstack = next(s for s in DEFAULT_SOURCES if s["name"] == "gstack")
        patterns = gstack.get("exclude_patterns") or []
        assert "projects/*/repo-mode.json" in patterns
        assert "projects/*/land-deploy-confirmed" in patterns
        assert "config.yaml" in patterns
        assert "analytics/.last-sync-*" in patterns

    def test_gstack_analytics_last_sync_glob_matches(self):
        """The `analytics/.last-sync-*` glob is meant to match both
        observed cursor files (`.last-sync-line`, `.last-sync-time`).
        Pin the fnmatch behavior so a future glob refactor can't silently
        narrow the match without breaking this test."""
        import fnmatch

        gstack = next(s for s in DEFAULT_SOURCES if s["name"] == "gstack")
        patterns = gstack.get("exclude_patterns") or []
        glob = "analytics/.last-sync-*"
        assert glob in patterns
        assert fnmatch.fnmatch("analytics/.last-sync-line", glob)
        assert fnmatch.fnmatch("analytics/.last-sync-time", glob)
        # And does NOT match the user-meaningful append-only logs in
        # the same directory.
        assert not fnmatch.fnmatch("analytics/skill-usage.jsonl", glob)
        assert not fnmatch.fnmatch("analytics/eureka.jsonl", glob)

    def test_gstack_extend_source_present(self):
        assert any(s["name"] == "gstack-extend" and s["type"] == "generic" for s in DEFAULT_SOURCES)

    def test_gstack_extend_include_dirs_scoped_to_projects(self):
        """`projects/` is the forward-compat slot for gstack-extend per-project state.
        Excluding the rest of `~/.gstack-extend/` by construction keeps per-machine
        bookkeeping (`config`, `just-upgraded-from`, `update-snoozed`) out of sync."""
        ext = next(s for s in DEFAULT_SOURCES if s["name"] == "gstack-extend")
        assert ext["include_dirs"] == ["projects"]
        assert ext["include_files"] == []

    def test_codex_source_syncs_only_durable_customization(self):
        codex = next(s for s in DEFAULT_SOURCES if s["name"] == "codex")
        assert codex["type"] == "generic"
        assert codex["include_dirs"] == ["skills", "plugins"]
        assert codex["include_files"] == ["AGENTS.md"]
        assert codex["exclude_patterns"] == [
            "skills/gstack-*",
            "skills/log-work/*",
            "skills/retro-fleet/*",
        ]

    def test_opencode_source_syncs_customization_not_session_state(self):
        opencode = next(s for s in DEFAULT_SOURCES if s["name"] == "opencode")
        assert opencode["type"] == "generic"
        assert opencode["include_dirs"] == [
            "agents",
            "commands",
            "modes",
            "plugins",
            "skills",
            "tools",
        ]
        assert opencode["include_files"] == ["AGENTS.md"]
        assert opencode["exclude_patterns"] == [
            "skills/gstack-*",
            "skills/log-work/*",
            "skills/retro-fleet/*",
        ]

    def test_grok_source_is_claude_shaped(self):
        grok = next(s for s in DEFAULT_SOURCES if s["name"] == "grok")
        assert grok == {"name": "grok", "path": "~/.grok", "type": "grok"}
        assert "include_dirs" not in grok
        assert "include_files" not in grok

    def test_generated_skill_excludes_preserve_hand_authored_skills(self):
        import fnmatch

        codex = next(s for s in DEFAULT_SOURCES if s["name"] == "codex")
        patterns = codex["exclude_patterns"]
        assert any(fnmatch.fnmatch("skills/gstack-review/SKILL.md", p) for p in patterns)
        assert any(fnmatch.fnmatch("skills/retro-fleet/SKILL.md", p) for p in patterns)
        assert not any(fnmatch.fnmatch("skills/my-team-tool/SKILL.md", p) for p in patterns)


class TestExcludePatternsValidation:
    """5C: _validate_sources accepts and validates the new
    exclude_patterns field at load time."""

    def test_valid_exclude_patterns_passes(self):
        sources = [
            {
                "name": "gstack",
                "path": "~/.gstack",
                "type": "generic",
                "exclude_patterns": ["projects/*/cache.json", "**/*.tmp"],
            }
        ]
        _validate_sources(sources)  # must not raise

    def test_no_exclude_patterns_field_passes(self):
        """A source without exclude_patterns is valid (the field is optional)."""
        sources = [{"name": "claude", "path": "~/.claude", "type": "claude"}]
        _validate_sources(sources)

    def test_empty_exclude_patterns_list_passes(self):
        sources = [
            {"name": "gstack", "path": "~/.gstack", "type": "generic", "exclude_patterns": []}
        ]
        _validate_sources(sources)

    def test_non_list_exclude_patterns_raises(self):
        with pytest.raises(ConfigError, match="exclude_patterns must be a list"):
            _validate_exclude_patterns("not-a-list", "gstack")

    def test_non_string_pattern_raises(self):
        with pytest.raises(ConfigError, match=r"exclude_patterns\[0\] must be a string"):
            _validate_exclude_patterns([42, "ok"], "gstack")

    def test_validate_sources_propagates_pattern_error(self):
        sources = [
            {
                "name": "gstack",
                "path": "~/.gstack",
                "type": "generic",
                "exclude_patterns": [42],
            }
        ]
        with pytest.raises(ConfigError, match="exclude_patterns"):
            _validate_sources(sources)

    def test_load_config_raises_on_bad_exclude_patterns_in_toml(self, tmp_path):
        """Headline: malformed exclude_patterns in TOML raises at load
        boundary, not mid-push."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[device]\n"
            'id = "abc"\n'
            'name = "Mac"\n'
            "[storage]\n"
            f'path = "{tmp_path / "storage"}"\n'
            "[[sync.sources]]\n"
            'name = "gstack"\n'
            'path = "~/.gstack"\n'
            'type = "generic"\n'
            'exclude_patterns = "should-be-a-list"\n'
        )
        with pytest.raises(ConfigError, match="exclude_patterns must be a list"):
            load_config(config_path)


class TestTomlValueEscaping:
    """5E ship-fix (F2): _toml_value MUST escape special characters in
    strings so user-customized exclude_patterns containing `"`, `\\`, or
    a newline survive `mm migrate-config --yes` without corrupting
    config.toml. Without escaping, the next `mm` invocation fails to
    parse and the install is bricked until the user manually edits."""

    def _parse_toml_value(self, val: str) -> object:
        """Parse a single TOML value via tomllib by wrapping in `k = <val>`."""
        import tomllib

        return tomllib.loads(f"k = {val}")["k"]

    def test_quote_in_string_round_trips_via_tomllib(self):
        from mind_meld.config import _toml_value

        result = _toml_value('foo"bar*')
        assert self._parse_toml_value(result) == 'foo"bar*'

    def test_backslash_in_string_round_trips_via_tomllib(self):
        from mind_meld.config import _toml_value

        result = _toml_value("with\\backslash")
        assert self._parse_toml_value(result) == "with\\backslash"

    def test_newline_in_string_round_trips_via_tomllib(self):
        from mind_meld.config import _toml_value

        result = _toml_value("line1\nline2")
        assert self._parse_toml_value(result) == "line1\nline2"

    def test_string_list_uses_per_element_escape(self):
        from mind_meld.config import _toml_value

        result = _toml_value(['foo"bar', "ok"])
        # Both strings are quoted; the embedded quote is escaped.
        assert '\\"' in result
        assert result.startswith("[")
        assert result.endswith("]")

    def test_round_trip_with_quote_in_exclude_pattern_does_not_corrupt(self, tmp_path):
        """Headline: save_config + load_config + patch_config_on_disk
        round-trip a glob containing a quote. F2 caught a real failure
        path: migrate-config wrote the value back with no escape, the
        next load raised ConfigError on parse."""
        config_path = tmp_path / "config.toml"
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {
                        "name": "gstack",
                        "path": str(tmp_path / ".gstack"),
                        "type": "generic",
                        "include_dirs": ["projects"],
                        # User wrote a custom exclude_pattern containing a quote.
                        "exclude_patterns": ['evil"name.txt', "back\\slash"],
                    }
                ],
            },
            "crypto": {"argon2_memory_kb": 1024},
        }
        save_config(config, config_path)
        # Round-trip through tomllib should succeed.
        loaded = load_config(config_path)
        src = loaded["sync"]["sources"][0]
        assert src["exclude_patterns"] == ['evil"name.txt', "back\\slash"]


class TestExcludePatternsRoundTrip:
    """exclude_patterns survives save → load → save."""

    def test_round_trip_preserves_exclude_patterns(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {
                        "name": "gstack",
                        "path": str(tmp_path / ".gstack"),
                        "type": "generic",
                        "include_dirs": ["projects"],
                        "exclude_patterns": [
                            "projects/*/repo-mode.json",
                            "projects/*/land-deploy-confirmed",
                        ],
                    }
                ],
            },
            "crypto": {"argon2_memory_kb": 1024},
        }
        save_config(config, config_path)
        loaded = load_config(config_path)
        src = loaded["sync"]["sources"][0]
        assert src["exclude_patterns"] == [
            "projects/*/repo-mode.json",
            "projects/*/land-deploy-confirmed",
        ]


class TestGetSources:
    def _base_config(self, tmp_path):
        """Minimal valid config with device and storage sections."""
        return {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
        }

    def test_returns_explicit_sources_when_present(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        config = self._base_config(tmp_path)
        config["sync"] = {
            "max_file_size": 52_428_800,
            "sources": [
                {"name": "claude", "path": str(claude_dir), "type": "claude"},
            ],
        }
        sources = get_sources(config)
        assert len(sources) == 1
        assert sources[0]["name"] == "claude"

    def test_falls_back_to_claude_dir(self, tmp_path, monkeypatch):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        # Monkeypatch home to prevent auto-detection of real ~/.gstack
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = self._base_config(tmp_path)
        config["sync"] = {
            "claude_dir": str(claude_dir),
            "max_file_size": 52_428_800,
        }
        sources = get_sources(config)
        assert len(sources) == 1
        assert sources[0]["name"] == "claude"
        assert sources[0]["type"] == "claude"

    def test_returns_default_sources_when_neither(self, tmp_path):
        config = self._base_config(tmp_path)
        # No sync section at all — should use DEFAULT_SOURCES
        # But only sources whose path exists will be returned.
        # Create the default paths so they show up.
        sources = get_sources(config)
        # Paths that don't exist on disk get filtered out, so this
        # may be empty on a test machine. Just verify no crash.
        assert isinstance(sources, list)

    def test_no_auto_detect_gstack_with_explicit_sources(self, tmp_path, monkeypatch):
        """When explicit sync.sources are defined, gstack auto-detection must NOT fire."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        gstack_dir = tmp_path / ".gstack"
        gstack_dir.mkdir()

        # Monkeypatch Path.home() so ~/.gstack resolves to our temp dir
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        config = self._base_config(tmp_path)
        config["sync"] = {
            "max_file_size": 52_428_800,
            "sources": [
                {"name": "claude", "path": str(claude_dir), "type": "claude"},
            ],
        }
        sources = get_sources(config)
        names = [s["name"] for s in sources]
        assert "gstack" not in names

    def test_auto_detects_gstack_with_claude_dir_fallback(self, tmp_path, monkeypatch):
        """When using claude_dir fallback and ~/.gstack exists, gstack gets auto-detected."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        gstack_dir = tmp_path / ".gstack"
        gstack_dir.mkdir()
        # Create dirs that gstack source expects
        (gstack_dir / "projects").mkdir()

        # Path.home() is used for the existence check; $HOME is read by
        # Path.expanduser() when resolving the "~/.gstack" default source
        # path. Patch both or a fresh CI runner's real $HOME leaks through.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))

        config = self._base_config(tmp_path)
        config["sync"] = {
            "claude_dir": str(claude_dir),
            "max_file_size": 52_428_800,
        }
        sources = get_sources(config)
        names = [s["name"] for s in sources]
        assert "claude" in names
        assert "gstack" in names

    def test_no_auto_detect_gstack_extend_with_explicit_sources(self, tmp_path, monkeypatch):
        """When explicit sync.sources are defined, gstack-extend auto-detection must NOT fire."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        gstack_extend_dir = tmp_path / ".gstack-extend"
        gstack_extend_dir.mkdir()

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        config = self._base_config(tmp_path)
        config["sync"] = {
            "max_file_size": 52_428_800,
            "sources": [
                {"name": "claude", "path": str(claude_dir), "type": "claude"},
            ],
        }
        sources = get_sources(config)
        names = [s["name"] for s in sources]
        assert "gstack-extend" not in names

    def test_auto_detects_gstack_extend_with_claude_dir_fallback(self, tmp_path, monkeypatch):
        """When using claude_dir fallback and ~/.gstack-extend exists, it gets auto-detected."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        ext_dir = tmp_path / ".gstack-extend"
        ext_dir.mkdir()
        # walk_generic_source tolerates a missing projects/ subdir, but the
        # source-path-existence filter requires the base path itself to exist.

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))

        config = self._base_config(tmp_path)
        config["sync"] = {
            "claude_dir": str(claude_dir),
            "max_file_size": 52_428_800,
        }
        sources = get_sources(config)
        names = [s["name"] for s in sources]
        assert "claude" in names
        assert "gstack-extend" in names

    def test_auto_detects_codex_and_opencode_with_claude_dir_fallback(self, tmp_path, monkeypatch):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (tmp_path / ".codex").mkdir()
        opencode_dir = tmp_path / ".config" / "opencode"
        opencode_dir.mkdir(parents=True)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))

        config = self._base_config(tmp_path)
        config["sync"] = {
            "claude_dir": str(claude_dir),
            "max_file_size": 52_428_800,
        }

        names = [source["name"] for source in get_sources(config)]
        assert "codex" in names
        assert "opencode" in names

    def test_does_not_auto_detect_grok_when_only_home_exists(self, tmp_path, monkeypatch):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (tmp_path / ".grok").mkdir()

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))

        config = self._base_config(tmp_path)
        config["sync"] = {
            "claude_dir": str(claude_dir),
            "max_file_size": 52_428_800,
        }
        names = [source["name"] for source in get_sources(config)]
        assert "grok" not in names

    def test_auto_detects_grok_when_a_customization_dir_exists(self, tmp_path, monkeypatch):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (tmp_path / ".grok" / "skills").mkdir(parents=True)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))

        config = self._base_config(tmp_path)
        config["sync"] = {
            "claude_dir": str(claude_dir),
            "max_file_size": 52_428_800,
        }
        names = [source["name"] for source in get_sources(config)]
        assert "grok" in names

    def test_default_sources_fallback_skips_grok_without_customization_dirs(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".grok").mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))

        config = self._base_config(tmp_path)
        config["sync"] = {"max_file_size": 52_428_800}
        names = [source["name"] for source in get_sources(config)]
        assert "grok" not in names

    def test_validates_source_configs(self):
        """Missing required fields in a source should raise ConfigError."""
        bad_sources = [{"name": "oops"}]  # missing path and type
        with pytest.raises(ConfigError, match="missing required field"):
            _validate_sources(bad_sources)

    def test_rejects_grok_generic_widening(self):
        with pytest.raises(ConfigError, match="must use type 'grok'"):
            _validate_sources(
                [
                    {
                        "name": "grok",
                        "path": "~/.grok",
                        "type": "generic",
                        "include_dirs": ["sessions"],
                    }
                ]
            )

    def test_rejects_grok_include_dirs(self):
        with pytest.raises(ConfigError, match="does not support include_dirs"):
            _validate_sources(
                [
                    {
                        "name": "grok",
                        "path": "~/.grok",
                        "type": "grok",
                        "include_dirs": ["sessions"],
                    }
                ]
            )

    @pytest.mark.parametrize("name", ["grok-custom", "alternate-grok"])
    def test_rejects_grok_type_aliases(self, name):
        with pytest.raises(ConfigError, match="reserved for source name 'grok'"):
            _validate_sources(
                [
                    {
                        "name": name,
                        "path": "~/.grok",
                        "type": "grok",
                    }
                ]
            )

    def test_filters_out_nonexistent_paths(self, tmp_path):
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(tmp_path / "nonexistent"), "type": "claude"},
                ],
            },
        }
        sources = get_sources(config)
        assert len(sources) == 0


class TestSaveConfigWithSources:
    def test_saves_and_reloads_with_sources_array(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config = {
            "device": {"id": "abc123", "name": "MacBook"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {"name": "claude", "path": str(tmp_path / ".claude"), "type": "claude"},
                    {
                        "name": "gstack",
                        "path": str(tmp_path / ".gstack"),
                        "type": "generic",
                        "include_dirs": ["projects", "analytics"],
                        "include_files": ["config.yaml", ".welcome-seen"],
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": 1024},
        }
        save_config(config, config_path)
        loaded = load_config(config_path)
        assert "sources" in loaded["sync"]
        loaded_sources = loaded["sync"]["sources"]
        assert len(loaded_sources) == 2
        assert loaded_sources[0]["name"] == "claude"
        assert loaded_sources[1]["name"] == "gstack"

    def test_round_trips_include_dirs_and_files(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {
                        "name": "test",
                        "path": str(tmp_path / ".test"),
                        "type": "generic",
                        "include_dirs": ["alpha", "beta"],
                        "include_files": ["one.yaml", "two.txt"],
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": 1024},
        }
        save_config(config, config_path)
        loaded = load_config(config_path)
        src = loaded["sync"]["sources"][0]
        assert src["include_dirs"] == ["alpha", "beta"]
        assert src["include_files"] == ["one.yaml", "two.txt"]

    def test_serializes_lists_as_toml_arrays(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "max_file_size": 52_428_800,
                "sources": [
                    {
                        "name": "src",
                        "path": str(tmp_path / ".src"),
                        "type": "generic",
                        "include_dirs": ["d1", "d2"],
                        "include_files": ["f1"],
                    },
                ],
            },
            "crypto": {"argon2_memory_kb": 1024},
        }
        save_config(config, config_path)
        raw = config_path.read_text()
        # TOML arrays look like ["d1", "d2"]
        assert '["d1", "d2"]' in raw
        assert '["f1"]' in raw


class TestEagerSourceValidation:
    """_validate runs _validate_sources so malformed sync.sources surfaces at load
    time instead of mid-sync."""

    def _base_config(self, tmp_path):
        return {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
        }

    def test_validate_passes_with_valid_sync_sources(self, tmp_path):
        config = self._base_config(tmp_path)
        config["sync"] = {"sources": [{"name": "claude", "path": "~/.claude", "type": "claude"}]}
        _validate(config)  # must not raise

    def test_validate_raises_on_source_missing_field(self, tmp_path):
        config = self._base_config(tmp_path)
        config["sync"] = {
            "sources": [{"name": "claude", "type": "claude"}]  # missing path
        }
        with pytest.raises(ConfigError, match="missing required field"):
            _validate(config)

    def test_validate_raises_on_duplicate_source_name(self, tmp_path):
        config = self._base_config(tmp_path)
        config["sync"] = {
            "sources": [
                {"name": "claude", "path": "~/.claude", "type": "claude"},
                {"name": "claude", "path": "/elsewhere", "type": "claude"},
            ]
        }
        with pytest.raises(ConfigError, match="duplicate source name"):
            _validate(config)

    def test_validate_passes_with_no_sync_sources_legacy(self, tmp_path):
        """Legacy configs with only claude_dir (no sources array) must still pass."""
        config = self._base_config(tmp_path)
        config["sync"] = {"claude_dir": "~/.claude"}
        _validate(config)  # must not raise

    def test_load_config_raises_on_bad_sources_in_toml(self, tmp_path):
        """Headline test: bad sync.sources in TOML raises at load boundary, not mid-push."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[device]\n"
            'id = "abc"\n'
            'name = "Mac"\n'
            "[storage]\n"
            f'path = "{tmp_path / "storage"}"\n'
            "[[sync.sources]]\n"
            'name = "claude"\n'
            'type = "claude"\n'
            # no path — eager validation should catch this
        )
        with pytest.raises(ConfigError, match="missing required field"):
            load_config(config_path)


class TestApplyDefaultsAfterLegacyCleanup:
    """_apply_defaults no longer defaults claude_dir; expansion is guarded and
    uses .resolve() to match the walker / storage pattern."""

    def test_no_claude_dir_injection_when_absent(self, tmp_path):
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
        }
        _apply_defaults(config)
        # claude_dir should NOT have been injected
        assert "claude_dir" not in config["sync"]
        # max_file_size still defaulted
        assert config["sync"]["max_file_size"] == 52_428_800

    def test_claude_dir_expanded_and_resolved_when_present(self, tmp_path):
        # Create a real dir so .resolve() has something concrete to canonicalize
        real_dir = tmp_path / "real_claude"
        real_dir.mkdir()
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {"claude_dir": str(real_dir)},
        }
        _apply_defaults(config)
        # Resolved form should match Path.resolve() output exactly
        assert config["sync"]["claude_dir"] == str(real_dir.resolve())

    def test_symlinked_claude_dir_stores_resolved_target(self, tmp_path):
        """If claude_dir is a symlink, .resolve() dereferences it.
        Regression test for consistency with walker/storage which also .resolve()."""
        real_dir = tmp_path / "real_claude"
        real_dir.mkdir()
        symlink = tmp_path / "link_claude"
        os.symlink(real_dir, symlink)

        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {"claude_dir": str(symlink)},
        }
        _apply_defaults(config)
        assert config["sync"]["claude_dir"] == str(real_dir.resolve())


class TestGetSourcesResolve:
    """get_sources must .resolve() source paths so they agree with walker-emitted paths."""

    def test_symlinked_source_path_is_resolved(self, tmp_path, monkeypatch):
        """Regression: get_sources dereferences symlinks on source paths."""
        real_dir = tmp_path / "real_claude"
        real_dir.mkdir()
        symlink = tmp_path / "link_claude"
        os.symlink(real_dir, symlink)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {"sources": [{"name": "claude", "path": str(symlink), "type": "claude"}]},
        }
        sources = get_sources(config)
        assert len(sources) == 1
        assert sources[0]["path"] == str(real_dir.resolve())

    def test_minimal_sync_block_falls_through_to_default_sources(self, tmp_path, monkeypatch):
        """Config with [sync] but no claude_dir and no sources must still work
        (no KeyError) and resolve via DEFAULT_SOURCES. Regression for legacy-cleanup."""
        # DEFAULT_SOURCES paths (~/.claude, ~/.gstack) go through
        # Path.expanduser() which reads $HOME, not Path.home(). Patch both
        # or a fresh CI runner's real $HOME leaks through and the default
        # claude source resolves to a non-existent /Users/runner/.claude.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        # Create the default claude path so it survives the existence filter
        (tmp_path / ".claude").mkdir()

        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {"max_file_size": 52_428_800},  # no claude_dir, no sources
        }
        # _apply_defaults must not KeyError on the missing claude_dir
        _apply_defaults(config)
        sources = get_sources(config)
        names = [s["name"] for s in sources]
        assert "claude" in names


class TestLoadSaveRoundTripIdempotency:
    """load → save → load on a symlinked path must produce the same resolved path.
    Codex-tension test: the review flagged that backfill save at cli.py:227 can
    silently rewrite user config paths. Test that at least the rewrite is stable."""

    def test_symlinked_claude_dir_round_trip_is_idempotent(self, tmp_path):
        real_dir = tmp_path / "real_claude"
        real_dir.mkdir()
        symlink = tmp_path / "link_claude"
        os.symlink(real_dir, symlink)

        config_path = tmp_path / "config.toml"
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {"claude_dir": str(symlink), "max_file_size": 52_428_800},
            "crypto": {"argon2_memory_kb": 1024},
        }
        # First save + load: symlink gets resolved on load
        save_config(config, config_path)
        loaded_1 = load_config(config_path)
        resolved_once = loaded_1["sync"]["claude_dir"]
        assert resolved_once == str(real_dir.resolve())

        # Save the (now-resolved) config, load again — must be identical
        save_config(loaded_1, config_path)
        loaded_2 = load_config(config_path)
        assert loaded_2["sync"]["claude_dir"] == resolved_once


class TestValidateSourcesShapeGuards:
    """_validate_sources raises ConfigError (not TypeError) on malformed input.
    Codex-tension tests: the bare _validate_sources assumed dict/list shapes."""

    def test_raises_on_non_list_sources(self):
        with pytest.raises(ConfigError, match="sync.sources must be a list"):
            _validate_sources("claude")  # string, not list

    def test_raises_on_non_dict_source_item(self):
        with pytest.raises(ConfigError, match="must be a table"):
            _validate_sources([42])  # list with non-dict item

    def test_raises_on_non_string_field_value(self):
        """Name/path/type must be strings. A list-valued name would otherwise
        hit TypeError at seen_names.add() and autopull/autopush would silently
        swallow it via the generic-Exception branch."""
        with pytest.raises(ConfigError, match="must be a string"):
            _validate_sources([{"name": ["claude"], "path": "~/.claude", "type": "claude"}])

    def test_raises_on_integer_path(self):
        with pytest.raises(ConfigError, match="must be a string"):
            _validate_sources([{"name": "claude", "path": 42, "type": "claude"}])


class TestDisabledSourcesField:
    """Per-machine source toggle (v0.10.0): [sync].disabled_sources filter
    in get_sources() + schema validation in _validate. The field is the
    source-resolution filter; the consumer-boundary tombstone-suppression
    invariant lives in cli.py (TestDisabledSourcesTombstoneSuppression)."""

    def _base_config(self, tmp_path) -> dict:
        return {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
        }

    def test_validate_passes_with_valid_disabled_sources(self, tmp_path):
        config = self._base_config(tmp_path)
        config["sync"] = {"disabled_sources": ["gstack"]}
        _validate(config)  # should not raise

    def test_validate_passes_with_empty_disabled_sources(self, tmp_path):
        config = self._base_config(tmp_path)
        config["sync"] = {"disabled_sources": []}
        _validate(config)

    def test_validate_passes_when_disabled_sources_absent(self, tmp_path):
        config = self._base_config(tmp_path)
        config["sync"] = {}
        _validate(config)

    def test_validate_raises_on_non_list(self, tmp_path):
        config = self._base_config(tmp_path)
        config["sync"] = {"disabled_sources": "gstack"}
        with pytest.raises(ConfigError, match="disabled_sources must be a list"):
            _validate(config)

    def test_validate_raises_on_non_string_entry(self, tmp_path):
        config = self._base_config(tmp_path)
        config["sync"] = {"disabled_sources": [42]}
        with pytest.raises(ConfigError, match="disabled_sources\\[0\\] must be a string"):
            _validate(config)

    def test_load_config_raises_on_bad_disabled_sources_in_toml(self, tmp_path):
        config_path = tmp_path / "config.toml"
        storage = tmp_path / "storage"
        storage.mkdir()
        config_path.write_text(
            f'[device]\nid = "abc"\nname = "Mac"\n'
            f'[storage]\npath = "{storage}"\n'
            f"[sync]\ndisabled_sources = 42\n"
        )
        with pytest.raises(ConfigError):
            load_config(config_path)


class TestGetSourcesDisabledFilter:
    """get_sources() filters by [sync].disabled_sources. Filter applies AFTER
    resolution (DEFAULT_SOURCES + auto-detect + explicit) and BEFORE the
    path-existence filter."""

    def test_disabled_sources_drops_named_source(self, tmp_path, monkeypatch):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        gstack_dir = tmp_path / ".gstack"
        gstack_dir.mkdir()
        # Both Path.home() AND $HOME need redirecting: get_sources uses
        # expanduser() which reads $HOME (not Path.home()), and the
        # auto-detect branch uses Path.home(). Patching only Path.home
        # passes locally if real ~/.claude exists, but fails in CI where
        # $HOME points at a clean /Users/runner with no ~/.claude.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))

        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {"disabled_sources": ["gstack"]},
        }
        sources = get_sources(config)
        names = [s["name"] for s in sources]
        assert "gstack" not in names
        assert "claude" in names

    def test_empty_disabled_sources_is_no_op(self, tmp_path, monkeypatch):
        """Regression: an empty disabled_sources list must not change behavior."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))

        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {"disabled_sources": []},
        }
        sources = get_sources(config)
        assert any(s["name"] == "claude" for s in sources)

    def test_unknown_name_in_disabled_sources_silently_ignored(self, tmp_path, monkeypatch):
        """Unknown names at this layer are silent — strict validation lives
        in the CLI (`mm disable-source <unknown>`). Validation here would
        force the CLI's --force escape hatch to live in a wrong layer."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))

        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {"disabled_sources": ["definitely-not-a-source"]},
        }
        sources = get_sources(config)
        assert any(s["name"] == "claude" for s in sources)

    def test_disabled_filter_applies_to_explicit_sources(self, tmp_path):
        """User has explicit [[sync.sources]] for claude + gstack; disable
        gstack: gstack is filtered out of resolution."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        gstack_dir = tmp_path / ".gstack"
        gstack_dir.mkdir()

        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(tmp_path / "storage")},
            "sync": {
                "sources": [
                    {"name": "claude", "path": str(claude_dir), "type": "claude"},
                    {"name": "gstack", "path": str(gstack_dir), "type": "generic"},
                ],
                "disabled_sources": ["gstack"],
            },
        }
        sources = get_sources(config)
        assert [s["name"] for s in sources] == ["claude"]


class TestDisabledSourcesRoundTrip:
    """save_config + load_config round-trips disabled_sources cleanly.
    Patch via patch_config_on_disk preserves [[sync.sources]] (array of
    tables) untouched."""

    def test_round_trip_disabled_sources(self, tmp_path):
        config_path = tmp_path / "config.toml"
        storage = tmp_path / "storage"
        storage.mkdir()
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(storage)},
            "sync": {"disabled_sources": ["gstack"]},
        }
        save_config(config, config_path)
        loaded = load_config(config_path)
        assert loaded["sync"]["disabled_sources"] == ["gstack"]

    def test_patch_disabled_sources_preserves_explicit_sources(self, tmp_path):
        """Patching [sync].disabled_sources via patch_config_on_disk must NOT
        clobber the [[sync.sources]] array of tables."""
        config_path = tmp_path / "config.toml"
        storage = tmp_path / "storage"
        storage.mkdir()
        claude_dir = tmp_path / "claude"
        claude_dir.mkdir()
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(storage)},
            "sync": {
                "sources": [
                    {"name": "claude", "path": str(claude_dir), "type": "claude"},
                ],
            },
        }
        save_config(config, config_path)

        patch_config_on_disk(
            {"sync": {"disabled_sources": ["gstack"]}},
            config_path,
        )

        loaded = load_config(config_path)
        assert loaded["sync"]["disabled_sources"] == ["gstack"]
        assert len(loaded["sync"]["sources"]) == 1
        assert loaded["sync"]["sources"][0]["name"] == "claude"


class TestLoadConfigNormalizesUnexpectedErrors:
    """load_config must normalize any non-ConfigError exception from validate or
    apply_defaults (e.g. .resolve() RuntimeError on cyclic symlinks) into
    ConfigError. Otherwise autopull/autopush fall through to the silent
    generic-Exception branch instead of surfacing the failure."""

    def test_unexpected_error_in_apply_defaults_becomes_config_error(self, tmp_path, monkeypatch):
        """Simulate a raw exception during _apply_defaults (e.g. what would happen
        if .resolve() hit a symlink loop) and verify it surfaces as ConfigError."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'[device]\nid = "abc"\nname = "Mac"\n[storage]\npath = "{tmp_path / "storage"}"\n'
        )

        def boom(*_args, **_kwargs):
            raise RuntimeError("simulated symlink loop")

        from mind_meld import config as config_module

        monkeypatch.setattr(config_module, "_apply_defaults", boom)

        with pytest.raises(ConfigError, match="failed to load"):
            load_config(config_path)


class TestConfigErrorPrefixes:
    """load_config's non-init prefixes must say 'config:' not 'init:' because
    they fire on every command that calls _get_config (push, pull, status,
    diag, recover...). 'init: config not found' stays — that branch genuinely
    points the user at running `mm init`."""

    def test_parse_error_prefix_is_config_not_init(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[device]\nid = "abc"\nname = "Mac"\n[storage\n'  # unclosed table header — parse error
        )
        with pytest.raises(ConfigError, match=r"^config: failed to parse"):
            load_config(config_path)

    def test_generic_load_error_prefix_is_config_not_init(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'[device]\nid = "abc"\nname = "Mac"\n[storage]\npath = "{tmp_path / "storage"}"\n'
        )

        def boom(*_args, **_kwargs):
            raise RuntimeError("simulated post-parse failure")

        from mind_meld import config as config_module

        monkeypatch.setattr(config_module, "_apply_defaults", boom)

        with pytest.raises(ConfigError, match=r"^config: failed to load"):
            load_config(config_path)

    def test_missing_file_error_prefix_still_init(self, tmp_path):
        """Pin: the missing-file branch still points at `mm init`."""
        with pytest.raises(ConfigError, match=r"^init: config not found"):
            load_config(tmp_path / "nonexistent.toml")


class TestUpdateConfigOnDisk:
    """patch_config_on_disk re-reads the raw TOML, merges a patch at section
    granularity, and persists. Critical invariant: untouched fields and
    untouched sections are byte-identical to what the user wrote.

    This is the backfill-save contract — first-run-after-upgrade persists
    crypto fingerprints WITHOUT silently rewriting `~/.claude` →
    `/Users/alice/.claude` or dereferencing symlinked storage paths."""

    def _write_minimal_config(self, config_path, storage_path_value):
        """Write a minimal valid config with the given storage.path value verbatim."""
        config_path.write_text(
            "[device]\n"
            'id = "abc"\n'
            'name = "Mac"\n'
            "[storage]\n"
            f'path = "{storage_path_value}"\n'
            "[sync]\n"
            'claude_dir = "~/.claude"\n'
            "max_file_size = 52428800\n"
        )

    def test_merges_into_existing_section(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[device]\n"
            'id = "abc"\n'
            'name = "Mac"\n'
            "[storage]\n"
            f'path = "{tmp_path / "s"}"\n'
            "[crypto]\n"
            "argon2_memory_kb = 1024\n"
        )
        patch_config_on_disk(
            {"crypto": {"root_salt_fp": "deadbeef"}},
            path=config_path,
        )
        loaded = load_config(config_path)
        assert loaded["crypto"]["root_salt_fp"] == "deadbeef"
        # Pre-existing field preserved
        assert loaded["crypto"]["argon2_memory_kb"] == 1024

    def test_creates_section_if_absent(self, tmp_path):
        """When the patched section doesn't exist on disk, helper creates it."""
        config_path = tmp_path / "config.toml"
        self._write_minimal_config(config_path, str(tmp_path / "s"))
        # No [crypto] section exists yet
        patch_config_on_disk(
            {"crypto": {"root_salt_fp": "x", "argon2_memory_kb": 65536}},
            path=config_path,
        )
        loaded = load_config(config_path)
        assert loaded["crypto"]["root_salt_fp"] == "x"
        assert loaded["crypto"]["argon2_memory_kb"] == 65536

    def test_preserves_tilde_storage_path(self, tmp_path):
        """CRITICAL REGRESSION: helper does NOT resolve '~/' in storage.path
        when persisting a patch to an unrelated section."""
        config_path = tmp_path / "config.toml"
        self._write_minimal_config(config_path, "~/Library/Mobile Documents/CloudDocs/mm")
        patch_config_on_disk(
            {"crypto": {"root_salt_fp": "x"}},
            path=config_path,
        )
        # Re-read raw TOML (not through load_config, which would mutate in memory)
        import tomllib

        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["storage"]["path"] == "~/Library/Mobile Documents/CloudDocs/mm"

    def test_preserves_symlinked_storage_path(self, tmp_path):
        """CRITICAL REGRESSION: helper does NOT dereference symlinks in
        storage.path when persisting a patch to an unrelated section."""
        real_dir = tmp_path / "real_storage"
        real_dir.mkdir()
        symlink = tmp_path / "link_storage"
        os.symlink(real_dir, symlink)

        config_path = tmp_path / "config.toml"
        self._write_minimal_config(config_path, str(symlink))
        patch_config_on_disk(
            {"crypto": {"root_salt_fp": "x"}},
            path=config_path,
        )
        import tomllib

        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["storage"]["path"] == str(symlink)

    def test_preserves_tilde_claude_dir(self, tmp_path):
        """CRITICAL REGRESSION: legacy sync.claude_dir survives backfill."""
        config_path = tmp_path / "config.toml"
        self._write_minimal_config(config_path, str(tmp_path / "s"))
        patch_config_on_disk(
            {"crypto": {"root_salt_fp": "x"}},
            path=config_path,
        )
        import tomllib

        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["sync"]["claude_dir"] == "~/.claude"

    def test_preserves_sync_sources_array_verbatim(self, tmp_path):
        """Modern multi-source config: sources array stays byte-identical."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[device]\n"
            'id = "abc"\n'
            'name = "Mac"\n'
            "[storage]\n"
            f'path = "{tmp_path / "s"}"\n'
            "[sync]\n"
            "max_file_size = 52428800\n"
            "[[sync.sources]]\n"
            'name = "claude"\n'
            'path = "~/.claude"\n'
            'type = "claude"\n'
            "[[sync.sources]]\n"
            'name = "gstack"\n'
            'path = "~/.gstack"\n'
            'type = "generic"\n'
            'include_dirs = ["projects", "analytics"]\n'
            'include_files = ["config.yaml"]\n'
        )
        patch_config_on_disk(
            {"crypto": {"root_salt_fp": "x"}},
            path=config_path,
        )
        import tomllib

        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["sync"]["sources"][0]["path"] == "~/.claude"
        assert raw["sync"]["sources"][1]["path"] == "~/.gstack"
        assert raw["sync"]["sources"][1]["include_dirs"] == ["projects", "analytics"]

    def test_raises_config_error_on_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="cannot update"):
            patch_config_on_disk(
                {"crypto": {"x": "y"}},
                path=tmp_path / "nonexistent.toml",
            )

    def test_patches_multiple_sections_independently(self, tmp_path):
        """Pin the dict-of-dicts contract: patches to multiple sections in a
        single call all land, each on its own section."""
        config_path = tmp_path / "config.toml"
        self._write_minimal_config(config_path, str(tmp_path / "s"))
        patch_config_on_disk(
            {
                "crypto": {"root_salt_fp": "abc"},
                "notify": {"email": "x@y.z"},
            },
            path=config_path,
        )
        loaded = load_config(config_path)
        assert loaded["crypto"]["root_salt_fp"] == "abc"
        assert loaded["notify"]["email"] == "x@y.z"

    def test_overwrites_existing_field_in_section(self, tmp_path):
        """A field update overwrites the on-disk value (same section, same
        key) without disturbing sibling fields. This is the backfill contract
        for argon2_memory_kb."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[device]\n"
            'id = "abc"\n'
            'name = "Mac"\n'
            "[storage]\n"
            f'path = "{tmp_path / "s"}"\n'
            "[crypto]\n"
            'root_salt_fp = "OLD"\n'
            "argon2_memory_kb = 1024\n"
        )
        patch_config_on_disk(
            {"crypto": {"root_salt_fp": "NEW"}},
            path=config_path,
        )
        loaded = load_config(config_path)
        assert loaded["crypto"]["root_salt_fp"] == "NEW"
        # Sibling field in the same section is untouched
        assert loaded["crypto"]["argon2_memory_kb"] == 1024

    def test_raises_config_error_on_malformed_toml(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text("[unclosed\n")
        with pytest.raises(ConfigError, match="failed to parse"):
            patch_config_on_disk(
                {"crypto": {"x": "y"}},
                path=config_path,
            )


class TestMmEventsSource:
    """Group 7 preflight #6 + D9: mm-events DEFAULT_SOURCES entry +
    get_sources() bootstrap. Codex outside-voice finding #9 flagged that
    without bootstrap, the source ships inert — get_sources() drops it
    via the path-existence filter at line 292. Bootstrap creates the
    base path on first call so the source is live from preflight ship.
    """

    def test_default_sources_includes_mm_events(self):
        names = [s["name"] for s in DEFAULT_SOURCES]
        assert "mm-events" in names

    @pytest.mark.no_mm_events_isolation
    def test_mm_events_default_shape(self):
        """Pinned shape of the mm-events default. Opts out of the autouse
        `_isolate_mm_events_path` fixture (which patches `.path` to a
        per-test tmp dir) so this test sees the canonical `~/.local/share`
        value it documents."""
        entry = next(s for s in DEFAULT_SOURCES if s["name"] == "mm-events")
        assert entry["type"] == "generic"
        assert entry["path"] == "~/.local/share/mind-meld"
        assert entry["include_dirs"] == ["events"]
        assert entry["exclude_patterns"] == []

    def test_mm_events_in_internal_source_names(self):
        from mind_meld.config import MM_INTERNAL_SOURCE_NAMES

        assert "mm-events" in MM_INTERNAL_SOURCE_NAMES

    @pytest.mark.no_mm_events_isolation
    def test_get_sources_bootstraps_mm_events_path(self, tmp_path, monkeypatch):
        """First get_sources() call on a fresh machine creates the
        mm-events base dir at mode 0700. Path-existence filter then
        keeps the source in the resolved list.

        Opts out of `_isolate_mm_events_path` so the canonical
        `~/.local/share/mind-meld` path is used; HOME redirection then
        keeps the bootstrap inside tmp_path."""
        # Redirect ~ to tmp_path so we don't pollute the real home dir.
        monkeypatch.setenv("HOME", str(tmp_path))
        config = {
            "device": {"id": "d1", "name": "Mac"},
            "storage": {"path": str(tmp_path / "icloud")},
            "sync": {},  # no explicit sources → DEFAULT_SOURCES applies
        }
        sources = get_sources(config)
        names = [s["name"] for s in sources]
        assert "mm-events" in names

        # Bootstrap created the directory at mode 0700.
        events_base = tmp_path / ".local" / "share" / "mind-meld"
        assert events_base.is_dir()
        # On macOS APFS the mode bits are tracked accurately; mask out the
        # high bits and check the bottom 9.
        assert (events_base.stat().st_mode & 0o777) == 0o700

    @pytest.mark.no_mm_events_isolation
    def test_bootstrap_idempotent(self, tmp_path, monkeypatch):
        """Re-calling get_sources() doesn't fail when the dir already exists."""
        monkeypatch.setenv("HOME", str(tmp_path))
        config = {
            "device": {"id": "d1", "name": "Mac"},
            "storage": {"path": str(tmp_path / "icloud")},
            "sync": {},
        }
        get_sources(config)
        # Second call must not raise; same source list.
        sources = get_sources(config)
        assert "mm-events" in [s["name"] for s in sources]

    @pytest.mark.no_mm_events_isolation
    def test_bootstrap_failure_emits_warning_and_drops_source(self, tmp_path, monkeypatch, capsys):
        """Permission denied on mkdir → mm: warning: stderr breadcrumb,
        source dropped via path-existence filter. Visible-failure contract
        per CLAUDE.md curated stderr taxonomy."""
        # Reset module-level warned-paths cache so this test is robust to
        # ordering against test_bootstrap_warns_once_per_process.
        from mind_meld import config as _config_module

        monkeypatch.setattr(_config_module, "_BOOTSTRAP_WARNED_PATHS", set())
        monkeypatch.setenv("HOME", str(tmp_path))
        # Make ~/.local/share read-only so mkdir of mind-meld/ fails.
        share = tmp_path / ".local" / "share"
        share.mkdir(parents=True)
        share.chmod(0o500)  # r-x — no write
        try:
            config = {
                "device": {"id": "d1", "name": "Mac"},
                "storage": {"path": str(tmp_path / "icloud")},
                "sync": {},
            }
            sources = get_sources(config)
            captured = capsys.readouterr()
            assert "mm: warning:" in captured.err
            assert "mm-events" in captured.err
            # Bootstrap failed → path doesn't exist → source dropped by
            # path-existence filter.
            names = [s["name"] for s in sources]
            assert "mm-events" not in names
        finally:
            # Restore so tmp cleanup can rm.
            share.chmod(0o755)

    @pytest.mark.no_mm_events_isolation
    def test_bootstrap_warns_once_per_process(self, tmp_path, monkeypatch, capsys):
        """Group 7 hotfix regression: chmod-restricted home must NOT spam
        `mm: warning:` on every read-only command. First call surfaces the
        breadcrumb (visible-failure contract); subsequent calls in the
        same process stay silent.

        Pinned by docs/ROADMAP.md Group 7 hotfix bullet — `_bootstrap_mm_events_path`
        is called from ~11 sites including read-only `mm sources` / `mm status` /
        `mm conflicts` / `mm diff` / `mm log`.
        """
        from mind_meld import config as _config_module

        monkeypatch.setattr(_config_module, "_BOOTSTRAP_WARNED_PATHS", set())
        monkeypatch.setenv("HOME", str(tmp_path))
        share = tmp_path / ".local" / "share"
        share.mkdir(parents=True)
        share.chmod(0o500)
        try:
            config = {
                "device": {"id": "d1", "name": "Mac"},
                "storage": {"path": str(tmp_path / "icloud")},
                "sync": {},
            }
            # Simulate 5 invocations (~roughly the read-only command-chain
            # observed when a user runs `mm status` in the wedged state).
            for _ in range(5):
                sources = get_sources(config)
                # Path-existence filter drops mm-events on every call (no
                # behavior change there) — the cache only suppresses the
                # warning emit + mkdir attempt.
                assert "mm-events" not in [s["name"] for s in sources]
            captured = capsys.readouterr()
            occurrences = captured.err.count("mm: warning: could not create mm-events")
            assert occurrences == 1, (
                f"expected exactly 1 warning across 5 get_sources() calls, "
                f"got {occurrences}; stderr was:\n{captured.err}"
            )
        finally:
            share.chmod(0o755)


class TestGrokHostUsageConfig:
    def test_absent_and_false_are_disabled(self):
        assert grok_host_usage_enabled({}) is False
        assert grok_host_usage_enabled({"retro": {}}) is False
        assert grok_host_usage_enabled({"retro": {"grok_host_usage": False}}) is False
        assert grok_host_usage_enabled({"retro": {"grok_host_usage": True}}) is True

    def test_non_bool_is_config_error(self):
        with pytest.raises(ConfigError, match="grok_host_usage must be a boolean"):
            _validate(
                {
                    "device": {"id": "abc123", "name": "Mac"},
                    "storage": {"path": "/tmp"},
                    "retro": {"grok_host_usage": "yes"},
                }
            )

    def test_unknown_retro_keys_are_not_rejected(self):
        _validate(
            {
                "device": {"id": "abc123", "name": "Mac"},
                "storage": {"path": "/tmp"},
                "retro": {"author_emails": ["a@example.com"], "grok_host_usage": True},
            }
        )

    def test_grok_is_a_default_sync_source(self):
        assert any(s["name"] == "grok" and s["type"] == "grok" for s in DEFAULT_SOURCES)

    def test_grok_sync_source_row_is_allowed(self):
        _validate(
            {
                "device": {"id": "abc123", "name": "Mac"},
                "storage": {"path": "/tmp"},
                "sync": {
                    "sources": [
                        {
                            "name": "grok",
                            "path": "~/.grok",
                            "type": "grok",
                        }
                    ]
                },
            }
        )


class TestSkillsSection:
    """[skills] maintain_links + agents. Track 25C."""

    def _base(self) -> dict:
        return {
            "device": {"id": "abc123", "name": "Mac"},
            "storage": {"path": "/tmp"},
        }

    def test_absent_table_is_valid(self):
        _validate(self._base())

    def test_maintain_links_true_and_false(self):
        cfg = self._base()
        cfg["skills"] = {"maintain_links": True}
        _validate(cfg)
        cfg["skills"] = {"maintain_links": False}
        _validate(cfg)

    def test_maintain_links_string_is_config_error(self):
        cfg = self._base()
        cfg["skills"] = {"maintain_links": "false"}
        with pytest.raises(ConfigError, match="skills.maintain_links must be a boolean"):
            _validate(cfg)

    def test_agents_list_of_str_accepted(self):
        cfg = self._base()
        cfg["skills"] = {"agents": ["codex", "nope"]}
        _validate(cfg)

    def test_agents_non_list_is_config_error(self):
        cfg = self._base()
        cfg["skills"] = {"agents": "codex"}
        with pytest.raises(ConfigError, match="skills.agents must be a list"):
            _validate(cfg)

    def test_agents_non_str_element_names_index(self):
        cfg = self._base()
        cfg["skills"] = {"agents": [42]}
        with pytest.raises(ConfigError, match=r"skills.agents\[0\] must be a string"):
            _validate(cfg)

    def test_empty_agents_is_config_error_naming_maintain_links(self):
        cfg = self._base()
        cfg["skills"] = {"agents": []}
        with pytest.raises(
            ConfigError, match="skills.agents must not be empty.*maintain_links = false"
        ):
            _validate(cfg)

    def test_skills_non_table_is_config_error(self):
        cfg = self._base()
        cfg["skills"] = "bad"
        with pytest.raises(ConfigError, match=r"\[skills\] must be a table, got str"):
            _validate(cfg)

    def test_unknown_agent_name_is_accepted(self):
        cfg = self._base()
        cfg["skills"] = {"agents": ["grok"]}
        _validate(cfg)

    def test_explicit_source_symlink_loop_is_a_config_error(self, tmp_path):
        loop = tmp_path / "source-loop"
        loop.symlink_to(loop)
        cfg = self._base()
        cfg["sync"] = {"sources": [{"name": "claude", "path": str(loop), "type": "claude"}]}

        with pytest.raises(ConfigError, match="failed to resolve source 'claude' path"):
            get_sources(cfg)

    def test_round_trip_preserves_both_keys(self, tmp_path):
        config_path = tmp_path / "config.toml"
        storage = tmp_path / "storage"
        storage.mkdir()
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": str(storage)},
            "skills": {"maintain_links": False, "agents": ["codex"]},
        }
        save_config(config, config_path)
        loaded = load_config(config_path)
        assert loaded["skills"]["maintain_links"] is False
        assert loaded["skills"]["agents"] == ["codex"]

    def test_patch_config_on_disk_writes_flat_skills_section(self, tmp_path):
        config_path = tmp_path / "config.toml"
        storage = tmp_path / "storage"
        storage.mkdir()
        save_config(
            {
                "device": {"id": "abc", "name": "Mac"},
                "storage": {"path": str(storage)},
            },
            config_path,
        )
        patch_config_on_disk(
            {"skills": {"maintain_links": True, "agents": ["claude", "codex"]}},
            config_path,
        )
        loaded = load_config(config_path)
        assert loaded["skills"]["maintain_links"] is True
        assert loaded["skills"]["agents"] == ["claude", "codex"]
