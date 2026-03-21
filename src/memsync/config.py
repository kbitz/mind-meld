"""Configuration management for MemSync.

Reads and writes ~/.config/memsync/config.toml.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from memsync.errors import ConfigError

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

CONFIG_DIR = Path.home() / ".config" / "memsync"
CONFIG_PATH = CONFIG_DIR / "config.toml"
LOCK_PATH = CONFIG_DIR / "memsync.lock"

# Defaults
DEFAULT_MAX_FILE_SIZE = 52_428_800  # 50MB
DEFAULT_ARGON2_MEMORY_KB = 65_536  # 64MB
DEFAULT_CLAUDE_DIR = str(Path.home() / ".claude")
DEFAULT_STORAGE_PATH = str(
    Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "memsync"
)

REQUIRED_FIELDS = {
    "device": ["id", "name"],
    "storage": ["path"],
}


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load and validate config.toml."""
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(
            f"init: config not found at {config_path} — run 'msync init' first."
        )
    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except Exception as e:
        raise ConfigError(f"init: failed to parse {config_path} — {e}") from e

    _validate(config)
    _apply_defaults(config)
    return config


def _validate(config: dict[str, Any]) -> None:
    """Check required fields exist."""
    for section, fields in REQUIRED_FIELDS.items():
        if section not in config:
            raise ConfigError(f"config: missing [{section}] section.")
        for field in fields:
            if field not in config[section]:
                raise ConfigError(f"config: missing {section}.{field}.")


def _apply_defaults(config: dict[str, Any]) -> None:
    """Fill in optional fields with defaults."""
    sync = config.setdefault("sync", {})
    sync.setdefault("claude_dir", DEFAULT_CLAUDE_DIR)
    sync.setdefault("max_file_size", DEFAULT_MAX_FILE_SIZE)

    crypto = config.setdefault("crypto", {})
    crypto.setdefault("argon2_memory_kb", DEFAULT_ARGON2_MEMORY_KB)

    # Expand ~ in paths
    config["storage"]["path"] = str(
        Path(config["storage"]["path"]).expanduser()
    )
    config["sync"]["claude_dir"] = str(
        Path(config["sync"]["claude_dir"]).expanduser()
    )


def save_config(config: dict[str, Any], path: Path | None = None) -> None:
    """Write config dict as TOML."""
    config_path = path or CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for section, values in config.items():
        if isinstance(values, dict):
            # Handle nested sections like sync.path_map
            simple = {k: v for k, v in values.items() if not isinstance(v, dict)}
            nested = {k: v for k, v in values.items() if isinstance(v, dict)}

            if simple:
                lines.append(f"[{section}]")
                for key, val in simple.items():
                    lines.append(f"{key} = {_toml_value(val)}")
                lines.append("")

            for sub_key, sub_values in nested.items():
                lines.append(f"[{section}.{sub_key}]")
                for key, val in sub_values.items():
                    lines.append(f"{key} = {_toml_value(val)}")
                lines.append("")

    config_path.write_text("\n".join(lines))


def _toml_value(val: Any) -> str:
    """Format a Python value as a TOML literal."""
    if isinstance(val, str):
        return f'"{val}"'
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return str(val)
    return f'"{val}"'
