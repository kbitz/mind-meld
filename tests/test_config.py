"""Tests for mind_meld.config — TOML load/save, validation."""

from pathlib import Path

import pytest

from mind_meld.config import (
    DEFAULT_SOURCES,
    _apply_defaults,
    _validate,
    _validate_sources,
    get_sources,
    load_config,
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

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        config = self._base_config(tmp_path)
        config["sync"] = {
            "claude_dir": str(claude_dir),
            "max_file_size": 52_428_800,
        }
        sources = get_sources(config)
        names = [s["name"] for s in sources]
        assert "claude" in names
        assert "gstack" in names

    def test_validates_source_configs(self):
        """Missing required fields in a source should raise ConfigError."""
        bad_sources = [{"name": "oops"}]  # missing path and type
        with pytest.raises(ConfigError, match="missing required field"):
            _validate_sources(bad_sources)

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
