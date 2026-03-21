"""Tests for memsync.config — TOML load/save, validation."""

import pytest

from memsync.config import _apply_defaults, _validate, load_config, save_config
from memsync.errors import ConfigError


class TestValidation:
    def test_valid_config(self):
        config = {
            "device": {"id": "abc123", "name": "MacBook"},
            "storage": {"path": "/tmp/memsync"},
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
            "storage": {"path": "/tmp/memsync"},
        }
        _apply_defaults(config)
        assert config["sync"]["max_file_size"] == 52_428_800
        assert config["crypto"]["argon2_memory_kb"] == 65_536

    def test_does_not_overwrite_existing(self):
        config = {
            "device": {"id": "abc", "name": "Mac"},
            "storage": {"path": "/tmp/memsync"},
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
