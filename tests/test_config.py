"""Tests for mind_meld.config — TOML load/save, validation."""

import os
from pathlib import Path

import pytest

from mind_meld.config import (
    _apply_defaults,
    _validate,
    _validate_sources,
    get_sources,
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
