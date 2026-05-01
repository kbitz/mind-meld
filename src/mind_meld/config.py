"""Configuration management for Mind Meld.

Reads and writes ~/.config/mind-meld/config.toml.
"""

from __future__ import annotations

import copy
import sys
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
# Source names that are mm-owned infrastructure (auto-included at init,
# not user-prompted). Per-machine opt-out remains via `mm disable-source`.
MM_INTERNAL_SOURCE_NAMES: frozenset[str] = frozenset({"mm-events"})

# Paths whose bootstrap mkdir has already failed in this process. Used by
# `_bootstrap_mm_events_path` to suppress repeat `mm: warning:` emit on
# chmod-restricted homes — the first failed call still surfaces the
# breadcrumb (visible-failure contract per CLAUDE.md), but the subsequent
# ~10 read-only command sites that re-call `get_sources()` stay silent.
# Per-path keying (not per-process) preserves the contract for the unlikely
# case of two failing mm-internal source paths. Tests touching the failure
# path must reset via `monkeypatch.setattr(config, "_BOOTSTRAP_WARNED_PATHS",
# set())` since this is module-level state.
_BOOTSTRAP_WARNED_PATHS: set[str] = set()

DEFAULT_SOURCES: list[dict[str, Any]] = [
    {"name": "claude", "path": DEFAULT_CLAUDE_DIR, "type": "claude"},
    {
        # mm-owned synced source for the per-device event log used by Group 8's
        # retro-fleet skill. Bootstrap creates the path on first get_sources()
        # call (so the source isn't inert until Track 7A's events.py first
        # writes — Group 7 preflight #6 + D9, codex outside-voice finding #9).
        # Per-device daily JSONL files land at events/<device>-<YYYY-MM-DD>.jsonl
        # under this base path. Subdir nesting plays cleanly with
        # walk_generic_source (avoids the include_dirs: ["."] pathlib quirk).
        "name": "mm-events",
        "path": "~/.local/share/mind-meld",
        "type": "generic",
        "include_dirs": ["events"],
        "exclude_patterns": [],
    },
    {
        "name": "gstack",
        "path": "~/.gstack",
        "type": "generic",
        "include_dirs": ["projects", "analytics", "retros"],
        # Curated to two categories: cross-machine memory content
        # (retro-context.md, greptile-history.md) and onboarding markers
        # (the dotfiles). Bare top-level files only — directories go in
        # include_dirs above. Adding here widens the default sync surface for
        # all new mm installs; existing configs with explicit [[sync.sources]]
        # need to opt in manually.
        "include_files": [
            "retro-context.md",
            "greptile-history.md",
            ".completeness-intro-seen",
            ".telemetry-prompted",
            ".proactive-prompted",
            ".welcome-seen",
            ".codex-desc-healed",
        ],
        # Per-machine artifacts that gstack recomputes on each device. Syncing
        # them produces a churning conflict file every pull (2026-04-24 first-pull regression).
        # repo-mode.json: 7-day TTL solo-vs-collaborative cache, recomputed
        # locally. land-deploy-confirmed: deploy-config-hash markers, computed
        # per-machine. config.yaml: holds gstack version-check tracking
        # (last successful version per machine); syncing it actively breaks
        # the version mechanism on whichever machine pulls last (v0.9.3).
        # analytics/.last-sync-*: per-machine cursor files (line + time) that
        # track each device's progress through gstack's local analytics
        # jsonls. Definitionally per-machine — syncing them produces a
        # conflict file on every pull from any peer (v0.11.13).
        # Excluding at the per-source glob level keeps the global EXCLUDED
        # list focused on universal junk (.git, *.tmp, etc.).
        "exclude_patterns": [
            "config.yaml",
            "projects/*/repo-mode.json",
            "projects/*/land-deploy-confirmed",
            "analytics/.last-sync-*",
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
    if isinstance(sync, dict):
        if "sources" in sync:
            _validate_sources(sync["sources"])
        if "disabled_sources" in sync:
            _validate_disabled_sources(sync["disabled_sources"])


def _apply_defaults(config: dict[str, Any]) -> None:
    """Fill in optional fields with defaults."""
    sync = config.setdefault("sync", {})
    sync.setdefault("max_file_size", DEFAULT_MAX_FILE_SIZE)

    crypto = config.setdefault("crypto", {})
    crypto.setdefault("argon2_memory_kb", DEFAULT_ARGON2_MEMORY_KB)

    # Auto-upgrade nudge (v0.9.4): on by default. Lenient validation —
    # unknown keys under [upgrade] are silently ignored (so a typo like
    # `auto_chec = false` never crashes a hook). Only the specific keys
    # we recognize are read by `mind_meld.upgrade`.
    upgrade = config.setdefault("upgrade", {})
    if "auto_check" not in upgrade or not isinstance(upgrade.get("auto_check"), bool):
        upgrade["auto_check"] = True

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
        if "exclude_patterns" in src:
            _validate_exclude_patterns(src["exclude_patterns"], name)


def _validate_disabled_sources(names: Any) -> None:
    """Check sync.disabled_sources is a list[str].

    Per-machine off-switch field (v0.10.0). Names need not match any
    currently-configured source — `--force` disable accepts unknown
    names for forward-compat (pre-disabling a not-yet-shipped source).
    Validation here is purely structural: shape errors at load time
    instead of crashing mid-sync on iteration.
    """
    if not isinstance(names, list):
        raise ConfigError(
            f"config: sync.disabled_sources must be a list, got {type(names).__name__}."
        )
    for j, name in enumerate(names):
        if not isinstance(name, str):
            raise ConfigError(
                f"config: sync.disabled_sources[{j}] must be a string, got {type(name).__name__}."
            )


def _validate_exclude_patterns(patterns: Any, source_name: str) -> None:
    """Check exclude_patterns is a list[str] of compilable fnmatch globs.

    fnmatch accepts any string as a pattern (no syntax errors), so we only
    enforce the structural shape (list of strings). This guards the malformed-
    schema branch (e.g. user wrote a single string instead of a list) at load
    time so push/pull don't crash mid-walk with a TypeError on iteration.
    """
    if not isinstance(patterns, list):
        raise ConfigError(
            f"config: source '{source_name}' exclude_patterns must be a list, "
            f"got {type(patterns).__name__}."
        )
    for j, pat in enumerate(patterns):
        if not isinstance(pat, str):
            raise ConfigError(
                f"config: source '{source_name}' exclude_patterns[{j}] must be "
                f"a string, got {type(pat).__name__}."
            )


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

    # Per-machine disabled toggle (v0.10.0): drop sources whose name is
    # in [sync].disabled_sources. The list is per-device — config.toml is
    # never synced — so this is naturally a per-machine preference.
    # See docs/designs/source-toggle.md.
    #
    # IMPORTANT: this is the source-resolution filter, NOT the consumer-
    # boundary filter. _push_core and _pull_core MUST also drop disabled
    # entries from prior_manifest / peer manifests respectively, before
    # tombstone computation, to prevent fleet-wide data loss on first
    # post-disable push. See cli.py and TestDisabledSourcesTombstoneSuppression.
    disabled = sync.get("disabled_sources", []) or []
    if disabled:
        sources = [s for s in sources if s["name"] not in disabled]

    # Bootstrap mm-owned source paths BEFORE the path-existence filter so
    # they don't fall through as "doesn't exist" on first run. The mm-events
    # source needs its base dir to exist for `walk_generic_source` to
    # consider it (Group 7 preflight #6 + D9, codex finding #9). Bootstrap
    # is mode 0700 (events contain device IDs and per-machine activity
    # metadata — not user-secret but per-machine-private). Failure emits
    # mm: warning: per the visible-failure contract; the source then drops
    # via the path-existence filter below.
    # Bootstrap registry: maps mm-internal source name → its bootstrap fn.
    # Adding a new entry to MM_INTERNAL_SOURCE_NAMES requires adding the
    # parallel bootstrap entry here; the dispatch by name keeps the
    # mapping explicit and prevents silent inconsistency between
    # _prompt_sources auto-include (cli.py) and bootstrap (here).
    bootstrap_dispatch: dict[str, Any] = {"mm-events": _bootstrap_mm_events_path}
    for src in sources:
        name = src.get("name")
        if name in MM_INTERNAL_SOURCE_NAMES and name in bootstrap_dispatch:
            bootstrap_dispatch[name](src["path"])

    # Filter to sources whose path exists on disk
    return [s for s in sources if Path(s["path"]).exists()]


def _bootstrap_mm_events_path(path: str) -> None:
    """Best-effort mkdir for the mm-events source base path.

    Idempotent — `exist_ok=True` makes re-call a no-op. Failure (permission
    denied on a chmod-restricted home, EROFS on a readonly mount) emits a
    single `mm: warning:` line to stderr per process per path and returns;
    the path-existence filter in get_sources will then drop the source from
    the resolved list.

    Warn-once via `_BOOTSTRAP_WARNED_PATHS`: chmod-restricted users see one
    breadcrumb on the first read-only command in a process, then silence
    for the ~10 subsequent `get_sources()` call sites. Visible-failure
    contract is preserved (monitoring catches the first occurrence).
    """
    p = Path(path).expanduser()
    if p.exists():
        return
    path_key = str(p)
    if path_key in _BOOTSTRAP_WARNED_PATHS:
        return
    try:
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as e:
        _BOOTSTRAP_WARNED_PATHS.add(path_key)
        sys.stderr.write(
            f"mm: warning: could not create mm-events source dir {p} ({type(e).__name__}: {e}); "
            "events will not be synced from this device\n"
        )


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
    """Format a Python value as a TOML literal.

    Strings are emitted as TOML basic strings (`"..."`) with `\\` and
    `"` escaped, plus literal CR/LF mapped to `\\r`/`\\n`. Without
    escaping, a user-supplied `exclude_patterns` value containing a
    quote (e.g. `foo"bar*`) would round-trip through `migrate-config`
    as a malformed TOML literal that wedges the next `mm` invocation
    on parse error (5E ship-fix; caught by /ship pre-landing review's
    adversarial pass).
    """
    if isinstance(val, str):
        escaped = (
            val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
        )
        return f'"{escaped}"'
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return str(val)
    if isinstance(val, list):
        if all(isinstance(v, str) for v in val):
            items = ", ".join(_toml_value(v) for v in val)
            return f"[{items}]"
        return str(val)  # fallback for non-string lists
    return _toml_value(str(val))
