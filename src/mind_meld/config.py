"""Configuration management for Mind Meld.

Reads and writes ~/.config/mind-meld/config.toml.
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

from mind_meld import fsutil
from mind_meld.errors import ConfigError

CONFIG_DIR = Path.home() / ".config" / "mind-meld"
CONFIG_PATH = CONFIG_DIR / "config.toml"
LOCK_PATH = CONFIG_DIR / "mind-meld.lock"

# Defaults. Paths use tilde-form so they round-trip through TOML readably.
# Expansion happens at use sites (get_sources, walker, storage) — keeping
# this as a literal avoids mutating user config on first-run-after-upgrade.
DEFAULT_MAX_FILE_SIZE = 52_428_800  # 50MB
DEFAULT_ARGON2_MEMORY_KB = 65_536  # 64MB
DEFAULT_CLAUDE_DIR = "~/.claude"
DEFAULT_STORAGE_PATH = str(
    Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "mind-meld"
)
DEFAULT_SOURCES: list[dict[str, Any]] = [
    {"name": "claude", "path": DEFAULT_CLAUDE_DIR, "type": "claude"},
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


def get_default_source(name: str) -> dict[str, Any] | None:
    """Return a deep copy of the DEFAULT_SOURCES entry matching name.

    Callers mutate the returned dict (inserting it into a user config),
    so deep-copy protects DEFAULT_SOURCES from aliasing pollution.
    Returns None if no default exists for this name.
    """
    for src in DEFAULT_SOURCES:
        if src["name"] == name:
            return copy.deepcopy(src)
    return None


REQUIRED_FIELDS = {
    "device": ["id", "name"],
    "storage": ["path"],
}


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load and validate config.toml."""
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(f"init: config not found at {config_path} — run 'mm init' first.")
    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except Exception as e:
        raise ConfigError(f"config: failed to parse {config_path} — {e}") from e

    # Normalize any non-ConfigError exceptions from validate/apply_defaults
    # (e.g. .resolve() RuntimeError on cyclic symlinks) into ConfigError so
    # autopull/autopush surface them via the typed-error branch instead of
    # falling through to the silent generic-Exception branch.
    try:
        _validate(config)
        _apply_defaults(config)
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"config: failed to load {config_path} — {e}") from e
    return config


def _validate(config: dict[str, Any]) -> None:
    """Check required fields exist."""
    for section, fields in REQUIRED_FIELDS.items():
        if section not in config:
            raise ConfigError(f"config: missing [{section}] section.")
        for field in fields:
            if field not in config[section]:
                raise ConfigError(f"config: missing {section}.{field}.")

    # Eager source validation: surface malformed sync.sources at load time
    # instead of mid-sync. Runs before _apply_defaults, so we only check
    # structure — path expansion happens in get_sources.
    sync = config.get("sync")
    if isinstance(sync, dict) and "sources" in sync:
        _validate_sources(sync["sources"])


def _apply_defaults(config: dict[str, Any]) -> None:
    """Fill in optional fields with defaults."""
    sync = config.setdefault("sync", {})
    sync.setdefault("max_file_size", DEFAULT_MAX_FILE_SIZE)

    crypto = config.setdefault("crypto", {})
    crypto.setdefault("argon2_memory_kb", DEFAULT_ARGON2_MEMORY_KB)

    # Canonicalize paths: expanduser + resolve matches the walker / storage pattern.
    # claude_dir is only present in legacy configs; guard the expansion accordingly.
    config["storage"]["path"] = str(Path(config["storage"]["path"]).expanduser().resolve())
    if "claude_dir" in sync:
        sync["claude_dir"] = str(Path(sync["claude_dir"]).expanduser().resolve())


def _validate_sources(sources: Any) -> None:
    """Check sync.sources is a list of dicts with required fields and unique names."""
    if not isinstance(sources, list):
        raise ConfigError(f"config: sync.sources must be a list, got {type(sources).__name__}.")
    seen_names: set[str] = set()
    for i, src in enumerate(sources):
        if not isinstance(src, dict):
            raise ConfigError(f"config: source #{i} must be a table, got {type(src).__name__}.")
        for field in ("name", "path", "type"):
            if field not in src:
                raise ConfigError(f"config: source #{i} missing required field '{field}'.")
            if not isinstance(src[field], str):
                raise ConfigError(
                    f"config: source #{i} field '{field}' must be a string, "
                    f"got {type(src[field]).__name__}."
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
            {**src, "path": str(Path(src["path"]).expanduser().resolve())}
            for src in sync["sources"]
        ]
    elif "claude_dir" in sync:
        sources = [
            {
                "name": "claude",
                "path": str(Path(sync["claude_dir"]).expanduser().resolve()),
                "type": "claude",
            }
        ]
    else:
        # DEFAULT_SOURCES paths (~/.claude, ~/.gstack) are only expanduser'd
        # here — the walker / storage re-resolve at use time, so skipping
        # .resolve() here keeps a cyclic user symlink (e.g. broken ~/.gstack)
        # from breaking get_sources for every command startup.
        sources = [{**src, "path": str(Path(src["path"]).expanduser())} for src in DEFAULT_SOURCES]

    # Auto-detect: append default gstack source if ~/.gstack exists but
    # no gstack source is already in the list.
    # Only auto-detect when NOT using explicit sync.sources config.
    gstack_path = Path.home() / ".gstack"
    has_gstack = any(s["name"] == "gstack" for s in sources)
    if not explicit_sources and gstack_path.exists() and not has_gstack:
        default_gstack = next((s for s in DEFAULT_SOURCES if s["name"] == "gstack"), None)
        if default_gstack:
            # Same rationale as the DEFAULT_SOURCES branch above: walker resolves
            # at use time, so no need to .resolve() a hardcoded ~/.gstack here
            # and risk cyclic-symlink failures at every command startup.
            sources.append(
                {
                    **default_gstack,
                    "path": str(Path(default_gstack["path"]).expanduser()),
                }
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


def patch_config_on_disk(updates: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    """Re-read TOML from disk, shallow-merge `updates` per field within each
    section, save.

    Used by backfill flows that need to persist a small patch (e.g. crypto
    session fingerprints after first-run-after-upgrade) WITHOUT writing
    `_apply_defaults`' in-memory path canonicalization back over the user's
    hand-edited TOML. `~/.claude` stays `~/.claude`; symlinks stay symlinks.

    Narrow contract — only for partial patches. This bypasses `_validate` /
    `_apply_defaults` by design, because the whole point is to preserve the
    raw user-authored text for fields outside the patch. For a full config
    write (fresh init, manual rewrite) use `save_config` directly.

    `updates` is shallow-keyed by section and shallow-valued by field:
    e.g. `{"crypto": {"root_salt_fp": "...", "argon2_memory_kb": 65536}}`.
    Sections not mentioned are untouched; fields not mentioned within a
    section are untouched. Sections absent from disk are created.

    The merge is section-level shallow: passing `{"sync": {"sources": [...]}}`
    REPLACES the existing sources array wholesale, not per-item. Array-of-tables
    sections (`[[sync.sources]]`) and nested tables cannot be partially
    patched through this helper — only flat key/value sections.

    Raises `ConfigError` on missing / malformed TOML or on a malformed
    on-disk section (e.g. `crypto = "bad"` where a table was expected).
    Callers in non-fatal backfill paths must swallow with their own try/except.
    """
    config_path = path or CONFIG_PATH
    try:
        with open(config_path, "rb") as f:
            on_disk = tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"config: cannot update — {config_path} does not exist.") from e
    except Exception as e:
        raise ConfigError(f"config: failed to parse {config_path} — {e}") from e

    for section, field_updates in updates.items():
        section_dict = on_disk.setdefault(section, {})
        if not isinstance(section_dict, dict):
            raise ConfigError(
                f"config: cannot patch — section [{section}] in {config_path} is not a table."
            )
        section_dict.update(field_updates)

    save_config(on_disk, config_path)


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
