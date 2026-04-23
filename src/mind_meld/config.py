"""Configuration management for Mind Meld.

Reads and writes ~/.config/mind-meld/config.toml.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from mind_meld import fsutil
from mind_meld.errors import ConfigError

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

CONFIG_DIR = Path.home() / ".config" / "mind-meld"
CONFIG_PATH = CONFIG_DIR / "config.toml"
LOCK_PATH = CONFIG_DIR / "mind-meld.lock"

# Defaults
DEFAULT_MAX_FILE_SIZE = 52_428_800  # 50MB
DEFAULT_ARGON2_MEMORY_KB = 65_536  # 64MB
DEFAULT_CLAUDE_DIR = str(Path.home() / ".claude")
DEFAULT_STORAGE_PATH = str(
    Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "mind-meld"
)
DEFAULT_SOURCES: list[dict[str, Any]] = [
    {"name": "claude", "path": "~/.claude", "type": "claude"},
    {
        "name": "gstack",
        "path": "~/.gstack",
        "type": "generic",
        "include_dirs": ["projects", "analytics", "retros"],
        "include_files": [
            "config.yaml",
            ".completeness-intro-seen",
            ".telemetry-prompted",
            ".proactive-prompted",
            ".welcome-seen",
            ".codex-desc-healed",
        ],
    },
]

REQUIRED_FIELDS = {
    "device": ["id", "name"],
    "storage": ["path"],
}


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load and validate config.toml."""
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(
            f"init: config not found at {config_path} — run 'mm init' first."
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


def _validate_sources(sources: list[dict[str, Any]]) -> None:
    """Check each source has required fields and unique names."""
    seen_names: set[str] = set()
    for i, src in enumerate(sources):
        for field in ("name", "path", "type"):
            if field not in src:
                raise ConfigError(
                    f"config: source #{i} missing required field '{field}'."
                )
        name = src["name"]
        if name in seen_names:
            raise ConfigError(f"config: duplicate source name '{name}'.")
        seen_names.add(name)


def get_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve the list of sync sources from config.

    Priority:
    1. config["sync"]["sources"] if present (explicit list)
    2. config["sync"]["claude_dir"] wrapped as a single claude source
    3. DEFAULT_SOURCES

    Auto-detection: if ~/.gstack/ exists on disk but no gstack source is
    in the resolved list, append the default gstack source.

    Finally, filter to sources whose path actually exists on disk.
    """
    sync = config.get("sync", {})

    explicit_sources = "sources" in sync
    if explicit_sources:
        sources = [
            {**src, "path": str(Path(src["path"]).expanduser())}
            for src in sync["sources"]
        ]
    elif "claude_dir" in sync:
        sources = [
            {
                "name": "claude",
                "path": str(Path(sync["claude_dir"]).expanduser()),
                "type": "claude",
            }
        ]
    else:
        sources = [
            {**src, "path": str(Path(src["path"]).expanduser())}
            for src in DEFAULT_SOURCES
        ]

    # Auto-detect: append default gstack source if ~/.gstack exists but
    # no gstack source is already in the list.
    # Only auto-detect when NOT using explicit sync.sources config.
    gstack_path = Path.home() / ".gstack"
    has_gstack = any(s["name"] == "gstack" for s in sources)
    if not explicit_sources and gstack_path.exists() and not has_gstack:
        default_gstack = next(
            (s for s in DEFAULT_SOURCES if s["name"] == "gstack"), None
        )
        if default_gstack:
            sources.append(
                {**default_gstack, "path": str(Path(default_gstack["path"]).expanduser())}
            )

    _validate_sources(sources)

    # Filter to sources whose path exists on disk
    return [s for s in sources if Path(s["path"]).exists()]


def save_config(config: dict[str, Any], path: Path | None = None) -> None:
    """Write config dict as TOML."""
    config_path = path or CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for section, values in config.items():
        if isinstance(values, dict):
            # Separate scalar/list values, nested dicts, and array-of-tables
            simple: dict[str, Any] = {}
            nested: dict[str, Any] = {}
            array_tables: dict[str, list[dict[str, Any]]] = {}

            for k, v in values.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    array_tables[k] = v
                elif isinstance(v, dict):
                    nested[k] = v
                else:
                    simple[k] = v

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

            # Serialize arrays of tables as [[section.key]]
            for arr_key, arr_items in array_tables.items():
                for item in arr_items:
                    lines.append(f"[[{section}.{arr_key}]]")
                    for key, val in item.items():
                        lines.append(f"{key} = {_toml_value(val)}")
                    lines.append("")

    # fsync=True: config corruption leaves the user unable to run mm.
    # Low call volume (only on save), so the durability cost is negligible.
    # 0600: the config contains device identity and storage paths —
    # internal state, not user-shared content.
    data = "\n".join(lines).encode("utf-8")
    fsutil.atomic_write_bytes(config_path, data, fsync=True, mode=0o600)


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
    if isinstance(val, list):
        if all(isinstance(v, str) for v in val):
            items = ", ".join(f'"{v}"' for v in val)
            return f"[{items}]"
        return str(val)  # fallback for non-string lists
    return f'"{val}"'
