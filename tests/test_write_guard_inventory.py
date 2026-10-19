"""Reviewed raw-filesystem census for Track 74A's finite writer boundary.

This is discovery and same-function ordering, not control-flow proof or a
sandbox. Behavioral refusal tests prove the named write paths. A new site,
including a second identical primitive in an existing function, needs review.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src/mind_meld"
PRIMITIVES = frozenset(
    {
        "open",
        "mkdir",
        "write_text",
        "write_bytes",
        "unlink",
        "touch",
        "link",
        "hardlink_to",
        "rename",
        "replace",
        "mkstemp",
        "symlink",
        "symlink_to",
        "makedirs",
        "removedirs",
        "rmdir",
        "chmod",
        "lchmod",
        "chown",
        "lchown",
        "utime",
        "truncate",
        "mkdtemp",
        "NamedTemporaryFile",
        "TemporaryFile",
        "SpooledTemporaryFile",
        "TemporaryDirectory",
        "renames",
        # Receiver-qualified: bare copy/move/connect names are too common to match.
        "shutil.rmtree",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.move",
        "shutil.make_archive",
        "shutil.unpack_archive",
        "sqlite3.connect",
    }
)
# fd-only operations (fchmod, ftruncate, write) act on descriptors from census-tracked opens.
QUALIFIED_MODULES = frozenset({"shutil", "sqlite3"})
CATEGORIES = frozenset({"guarded-here", "read-only", "own-guard", "residual"})


WRITE_FLAGS = frozenset({"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND", "O_TMPFILE"})


def _callee(function, aliases):
    """Primitive name for a call target, normalizing os.remove and qualified modules."""
    if isinstance(function, ast.Name):
        return aliases.get(function.id, function.id)
    if not isinstance(function, ast.Attribute):
        return ""
    receiver = (
        aliases.get(function.value.id, function.value.id)
        if isinstance(function.value, ast.Name)
        else ""
    )
    # os.remove is another spelling of unlink; list/set.remove is not.
    if receiver == "os" and function.attr == "remove":
        return "unlink"
    qualified = f"{receiver}.{function.attr}"
    # Qualified names not listed fall back to the bare name (shutil.chown -> chown).
    return qualified if qualified in PRIMITIVES else function.attr


def _inventory(source, module):
    tree = ast.parse(source)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                aliases[imported.asname or imported.name] = imported.name
        elif isinstance(node, ast.ImportFrom) and node.module in {
            "os",
            "io",
            "builtins",
            "tempfile",
            "pathlib",
            *QUALIFIED_MODULES,
        }:
            for imported in node.names:
                name = imported.name
                if node.module == "os" and name == "remove":
                    name = "unlink"
                elif f"{node.module}.{name}" in PRIMITIVES:
                    name = f"{node.module}.{name}"
                aliases[imported.asname or imported.name] = name
        elif isinstance(node, ast.Assign):
            original = _callee(node.value, aliases)
            if original in PRIMITIVES:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = original
    sites, guards, write_flags = [], {}, set()

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def owner(self):
            return module + ":" + ".".join(self.stack)

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Attribute(self, node):
            if node.attr in WRITE_FLAGS:
                write_flags.add(self.owner())
            self.generic_visit(node)

        def visit_Name(self, node):
            if aliases.get(node.id, node.id) in WRITE_FLAGS:
                write_flags.add(self.owner())

        def visit_Call(self, node):
            name = _callee(node.func, aliases)
            if name == "refuse_real_state_write_under_pytest":
                guards.setdefault(self.owner(), []).append(node.lineno)
            if name in PRIMITIVES:
                sites.append((self.owner(), ast.unparse(node), node.lineno, name))
            self.generic_visit(node)

    Visitor().visit(tree)
    return sites, guards, write_flags


# Exact expression multisets, deliberately independent of line numbers. "open"
# includes read-only calls and variable flag/mode expressions so a mode change
# cannot silently evade discovery. replace also includes reviewed string/date
# operations; conservative discovery avoids pretending to infer receiver types.
# Each row: classification, reason, reviewed primitive expressions.
_GUARDED = ("guarded-here", "fsutil refusal before mutation; behavioral guard tests pin placement")
_READ_ONLY = (
    "read-only",
    "read-only file/descriptor open or in-memory string/date/dataclass replacement",
)
_OWN_GUARD = (
    "own-guard",
    "installer bool guard on push; direct symlink/unlink gap remains documented",
)

REVIEWED = {
    "attemptlog.py:write": (
        *_GUARDED,
        [
            "sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)",
        ],
    ),
    "attemptlog.py:read": (
        *_READ_ONLY,
        [
            "record_path().open('rb')",
        ],
    ),
    "cli.py:_retain_prior_default_sources": (
        *_READ_ONLY,
        [
            "replace(resolution, selected=selected, available=list(resolution.available))",
        ],
    ),
    "cli.py:_apply_conflict": (
        "residual",
        "stale sidecar cleanup; creation guarded, raw unlink outside frozen inventory",
        [
            "sidecar.unlink()",
        ],
    ),
    "cli.py:_apply_incoming_file": (
        *_GUARDED,
        [
            "directory.mkdir(exist_ok=True)",
        ],
    ),
    "cli.py:init": (
        *_GUARDED,
        [
            "full_path.mkdir(parents=True, exist_ok=True)",
        ],
    ),
    "cli.py:_push_core": (
        *_READ_ONLY,
        [
            (
                "replace(resolution, selected=[src for src in res"
                "olution.selected if src not in skipped_internal])"
            ),
        ],
    ),
    "cli.py:cursor_agent.emit": (
        "residual",
        "os.devnull output sink, not an account-state file",
        [
            "os.open(os.devnull, os.O_WRONLY)",
        ],
    ),
    "cli.py:_migrate_config_core": (
        "residual",
        "migration breadcrumb cleanup; raw unlink outside frozen inventory",
        [
            "breadcrumb.unlink()",
        ],
    ),
    "cli.py:_oldest_commit_date": (
        *_READ_ONLY,
        [
            "ts.replace(tzinfo=timezone.utc)",
        ],
    ),
    "cli.py:log_cmd": (
        *_READ_ONLY,
        [
            "since_dt.replace(tzinfo=timezone.utc)",
        ],
    ),
    "cli.py:_quarantine_corrupt_manifest": (
        "residual",
        "quarantine source unlink after guarded copy; frozen inventory residual",
        [
            "os.unlink(src)",
        ],
    ),
    "cli.py:_breadcrumb_staleness_suffix": (
        *_READ_ONLY,
        [
            "ts.replace('Z', '+00:00')",
            "when.replace(tzinfo=timezone.utc)",
        ],
    ),
    "cli.py:_write_migration_breadcrumb": (
        *_GUARDED,
        [
            "path.unlink()",
            "sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)",
            "path.write_text(_json.dumps(payload, indent=2))",
        ],
    ),
    "cli.py:_log_unexpected": (
        *_GUARDED,
        [
            "sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)",
            "open(path, 'a+b')",
            "f.truncate()",
        ],
    ),
    "config.py:load_config": (
        *_READ_ONLY,
        [
            "open(config_path, 'rb')",
        ],
    ),
    "config.py:_bootstrap_mm_events_path": (
        *_GUARDED,
        [
            "directory.mkdir(mode=448)",
            "os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)",
        ],
    ),
    "config.py:save_config": (
        *_GUARDED,
        [
            "config_path.parent.mkdir(parents=True, exist_ok=True)",
        ],
    ),
    "config.py:patch_config_on_disk": (
        *_READ_ONLY,
        [
            "open(config_path, 'rb')",
        ],
    ),
    "config.py:_toml_value": (
        *_READ_ONLY,
        [
            (
                "val.replace('\\\\', '\\\\\\\\').replace('\"', '\\\\\""
                "').replace('\\n', '\\\\n').replace('\\r', '\\\\r')"
            ),
            "val.replace('\\\\', '\\\\\\\\').replace('\"', '\\\\\"').replace('\\n', '\\\\n')",
            "val.replace('\\\\', '\\\\\\\\').replace('\"', '\\\\\"')",
            "val.replace('\\\\', '\\\\\\\\')",
        ],
    ),
    "conflictmtime.py:_restore_mtime_best_effort": (
        "residual",
        "mtime-only restore outside frozen inventory; reached after guarded apply writes",
        [
            "os.utime(path, (ts, ts))",
        ],
    ),
    "conflictmtime.py:_bump_canonical_mtime_post_resolve": (
        "residual",
        "mtime-only bump on a canonical file outside frozen inventory",
        [
            "os.utime(canonical, (target, target))",
        ],
    ),
    "crypto.py:_read_regular_nofollow": (
        *_READ_ONLY,
        [
            "os.open(path, flags)",
        ],
    ),
    "crypto.py:fetch_crypto_init": (
        *_READ_ONLY,
        [
            (
                "replace(winner, winner_bytes=winner_raw, repair_plan=p"
                "lan if plan.replace_canonical or plan.copies else None)"
            ),
        ],
    ),
    "crypto.py:apply_crypto_init_repair": (
        "residual",
        "conflict-copy deletion after verified repair; raw unlink residual",
        [
            "path.unlink()",
        ],
    ),
    "devices.py:_devices_write_lock": (
        *_GUARDED,
        [
            "DEVICES_WRITE_LOCK.parent.mkdir(parents=True, exist_ok=True)",
            "os.open(str(DEVICES_WRITE_LOCK), os.O_WRONLY | os.O_CREAT, 384)",
        ],
    ),
    "events.py:_read_cwd_from_latest_jsonl": (
        *_READ_ONLY,
        [
            "open(jsonl_path, 'rb')",
        ],
    ),
    "events.py:_iter_typed_objs": (
        *_READ_ONLY,
        [
            "open(path, 'rb')",
        ],
    ),
    "events.py:recorded_commit_keys": (
        *_READ_ONLY,
        [
            "open(path, 'rb')",
        ],
    ),
    "fsutil.py:atomic_write_bytes": (
        *_GUARDED,
        [
            "tempfile.mkstemp(dir=parent, suffix='.tmp')",
            "os.chmod(tmp_name, mode)",
            "os.replace(tmp_name, path)",
            "os.unlink(tmp_name)",
        ],
    ),
    "fsutil.py:flock_append_jsonl": (
        *_GUARDED,
        [
            "path.parent.mkdir(parents=True, exist_ok=True, mode=448)",
            "os.open(str(path), flags, mode)",
            "os.open(str(path), flags | os.O_CREAT | os.O_EXCL, mode)",
            "os.open(str(path), flags, mode)",
        ],
    ),
    "fsutil.py:append_rotatable_jsonl": (
        *_GUARDED,
        [
            "path.parent.mkdir(parents=True, exist_ok=True, mode=448)",
            "os.open(str(path), os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, mode)",
        ],
    ),
    "fsutil.py:rotate_jsonl": (
        *_GUARDED,
        [
            "os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)",
            "os.rename(path, destination)",
        ],
    ),
    "fsutil.py:fsync_dir": (
        *_READ_ONLY,
        [
            "os.open(str(path), os.O_RDONLY)",
        ],
    ),
    "host_usage.py:_read_cursor_sqlite": (
        "read-only",
        "read-only SQLite URI (mode=ro); its RuntimeError guard refuses the real Conductor store",
        [
            "sqlite3.connect(uri, uri=True, isolation_level=None, "
            "timeout=min(remaining, _CURSOR_SQLITE_BUSY_S))",
        ],
    ),
    "host_usage.py:_read_cursor_file": (
        *_READ_ONLY,
        [
            "os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)",
        ],
    ),
    "host_usage.py:_sqlite_day": (
        *_READ_ONLY,
        [
            "datetime.strptime(text, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)",
        ],
    ),
    "host_usage.py:read_cursor_usage": (
        "residual",
        "isolated Cursor spool cleanup; raw unlink residual",
        [
            "_cursor_spool_merging_path().unlink()",
        ],
    ),
    "host_usage.py:_read_bounded_bytes": (
        *_READ_ONLY,
        [
            "os.open(path, flags)",
        ],
    ),
    "host_usage.py:_take_cursor_spool": (
        "residual",
        "isolated Cursor spool recovery; raw unlink residual",
        [
            "merging.unlink()",
        ],
    ),
    "host_usage.py:_read_grok_file": (
        *_READ_ONLY,
        [
            "path.open('rb')",
        ],
    ),
    "host_usage.py:_read_muse_file": (
        *_READ_ONLY,
        [
            "path.open('rb')",
        ],
    ),
    "host_usage.py:_utc_day": (
        *_READ_ONLY,
        [
            "value.replace('Z', '+00:00')",
        ],
    ),
    "host_usage.py:_read_rollout": (
        *_READ_ONLY,
        [
            "path.open('rb')",
        ],
    ),
    "host_usage.py:_record_day": (
        *_READ_ONLY,
        [
            "timestamp.replace('Z', '+00:00')",
        ],
    ),
    "host_usage.py:_fingerprint": (
        *_READ_ONLY,
        [
            "path.open('rb')",
        ],
    ),
    "host_usage.py:_digest_range": (
        *_READ_ONLY,
        [
            "path.open('rb')",
        ],
    ),
    "identity.py:_is_cache_stale": (
        *_READ_ONLY,
        [
            "refreshed_at.replace('Z', '+00:00')",
            "dt.replace(tzinfo=timezone.utc)",
        ],
    ),
    "lockedjson.py:locked_json_durable_rmw": (
        *_GUARDED,
        [
            "path.parent.mkdir(parents=True, exist_ok=True, mode=448)",
            "os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 384)",
            "os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)",
        ],
    ),
    "lockedjson.py:locked_json_rmw": (
        *_GUARDED,
        [
            "path.parent.mkdir(parents=True, exist_ok=True)",
            "os.open(str(path), os.O_RDWR | os.O_CREAT, mode)",
        ],
    ),
    "lockedjson.py:locked_json_snapshot": (
        *_READ_ONLY,
        [
            "os.open(str(path), os.O_RDONLY)",
        ],
    ),
    "lockfile.py:acquire_lock": (
        *_GUARDED,
        [
            "lock_path.parent.mkdir(parents=True, exist_ok=True)",
            "os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 420)",
        ],
    ),
    "manifest.py:parse_conflict_created_at": (
        *_READ_ONLY,
        [
            "datetime.strptime(stamp, '%Y%m%d-%H%M%S').replace(tzinfo=timezone.utc)",
        ],
    ),
    "manifest.py:_open_nofollow_nonblock": (
        *_READ_ONLY,
        [
            "os.open(path, flags)",
        ],
    ),
    "manifest.py:hash_file": (
        *_READ_ONLY,
        [
            "open(path, 'rb')",
        ],
    ),
    "manifest.py:_validate_rel_path": (
        *_READ_ONLY,
        [
            "rel.replace('\\\\', '/')",
        ],
    ),
    "manifest.py:_is_active_tombstone": (
        *_READ_ONLY,
        [
            "ts.replace(tzinfo=timezone.utc)",
        ],
    ),
    "pullhistory.py:_timestamp_us": (
        *_READ_ONLY,
        [
            "stamp.replace(tzinfo=timezone.utc)",
        ],
    ),
    "pullhistory.py:_yield_lines": (
        *_READ_ONLY,
        [
            "open(path, 'rb')",
        ],
    ),
    "resolveflow.py:_ensure_inversion_marker": (
        *_GUARDED,
        [
            "sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)",
        ],
    ),
    "resolveflow.py:_migrate_pre_inversion_conflict": (
        "residual",
        "migration rename (replaces any target) outside frozen inventory",
        [
            "path.rename(new_path)",
        ],
    ),
    "resolveflow.py:_promote_conflict_file": (
        "residual",
        "promotion link creates canonical content; link/unlink outside frozen inventory",
        [
            "os.link(cpath, target)",
            "os.link(cpath, target)",
            "os.unlink(cpath)",
        ],
    ),
    "resolveflow.py:_resolve_interactive_loop": (
        "residual",
        "interactive resolution renames overwrite canonical files; outside frozen inventory",
        [
            "cpath.rename(target_canonical)",
            "cpath.unlink()",
            "cpath.rename(canonical)",
            "cpath.unlink()",
            "cpath.unlink()",
            "cpath.rename(canonical)",
            "cpath.unlink()",
        ],
    ),
    "retention.py:_sweep_local_tmp_files": (
        "residual",
        "retention unlink outside frozen inventory",
        [
            "v.unlink()",
        ],
    ),
    "retention.py:_gc_old_event_files": (
        "residual",
        "retention unlink outside frozen inventory",
        [
            "path.unlink()",
        ],
    ),
    "retention.py:_gc_old_conflict_files": (
        "residual",
        "retention unlink outside frozen inventory",
        [
            "cpath.unlink()",
        ],
    ),
    "retention.py:_gc_orphan_retros_dir": (
        "residual",
        "retention unlink and empty-directory rmdir outside frozen inventory",
        [
            "path.unlink()",
            "target.rmdir()",
        ],
    ),
    "seen_sources.py:read": (
        *_GUARDED,
        [
            "os.open(str(path), os.O_RDONLY)",
            "SEEN_DIR.mkdir(parents=True, exist_ok=True)",
            "os.open(str(path), os.O_RDWR | os.O_CREAT, 384)",
        ],
    ),
    "seen_sources.py:acknowledge": (
        *_GUARDED,
        [
            "SEEN_DIR.mkdir(parents=True, exist_ok=True)",
            "os.open(str(path), os.O_RDWR | os.O_CREAT, 384)",
        ],
    ),
    "seen_sources.py:write": (
        *_GUARDED,
        [
            "SEEN_DIR.mkdir(parents=True, exist_ok=True)",
        ],
    ),
    "sidecar.py:write": (
        *_GUARDED,
        [
            "SIDECAR_DIR.mkdir(parents=True, exist_ok=True)",
        ],
    ),
    "skill_link.py:_ensure_real_dir": (
        *_OWN_GUARD,
        [
            "path.mkdir(mode=mode, parents=False)",
        ],
    ),
    "skill_link.py:_prepare_store_dir": (
        *_OWN_GUARD,
        [
            "grand.mkdir(mode=448, parents=True)",
        ],
    ),
    "skill_link.py:_store_publish_lock": (
        *_OWN_GUARD,
        [
            "os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 420)",
        ],
    ),
    "skill_link.py:_publish_skill_store": (
        *_OWN_GUARD,
        [
            "store.parent.parent.mkdir(mode=448, parents=True, exist_ok=True)",
            "store.parent.mkdir(mode=448, exist_ok=True)",
        ],
    ),
    "skill_link.py:_replace_symlink": (
        *_OWN_GUARD,
        [
            "os.unlink(tmp)",
            "os.symlink(str(store), tmp)",
            "os.unlink(tmp)",
            "os.replace(tmp, target)",
            "os.unlink(tmp)",
        ],
    ),
    "skill_link.py:_reap_retired_opencode_skill_link": (
        *_OWN_GUARD,
        [
            "os.unlink(target)",
            "os.unlink(marker_dir / f'.{name}')",
        ],
    ),
    "skill_link.py:_install_available_skill_target": (
        *_OWN_GUARD,
        [
            "descriptor.skills_dir.mkdir(mode=448)",
            "os.symlink(str(store), target)",
        ],
    ),
    "skill_link.py:_touch_marker": (
        *_OWN_GUARD,
        [
            "marker_dir.mkdir(mode=448, parents=True, exist_ok=True)",
            "(marker_dir / f'.{name}').touch()",
        ],
    ),
    "skills/retro_fleet/aggregator.py:_iter_jsonl": (
        *_READ_ONLY,
        [
            "open(path, encoding='utf-8', errors='replace')",
        ],
    ),
    "skills/retro_fleet/aggregator.py:_parse_iso": (
        *_READ_ONLY,
        [
            "ts.replace('Z', '+00:00')",
            "dt.replace(tzinfo=timezone.utc)",
        ],
    ),
    "skills/retro_fleet/aggregator.py:_parse_aware_ts": (
        *_READ_ONLY,
        [
            "ts.replace('Z', '+00:00')",
        ],
    ),
    "statedb.py:connect": (
        *_GUARDED,
        [
            "path.parent.mkdir(parents=True, exist_ok=True, mode=448)",
            "os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 384)",
            "sqlite3.connect(uri, uri=True, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)",
            "os.chmod(path, 384)",
        ],
    ),
    "storage/local.py:LocalBackend.put": (
        *_GUARDED,
        [
            "path.parent.mkdir(parents=True, exist_ok=True)",
        ],
    ),
    "storage/local.py:LocalBackend.put_exclusive": (
        *_GUARDED,
        [
            "path.parent.mkdir(parents=True, exist_ok=True)",
            "tempfile.mkstemp(dir=path.parent, suffix='.tmp')",
            "os.link(tmp_path, path)",
            "os.unlink(tmp_path)",
            "os.unlink(tmp_path)",
            "os.unlink(tmp_path)",
        ],
    ),
    "storage/local.py:LocalBackend.delete": (
        *_GUARDED,
        [
            "path.unlink()",
        ],
    ),
    "storage/local.py:LocalBackend.delete_conflict_copies": (
        *_GUARDED,
        [
            "c.unlink(missing_ok=True)",
        ],
    ),
    "store/format.py:seal_record": (
        *_READ_ONLY,
        [
            "replace(expected, part=part, nparts=nparts)",
        ],
    ),
    "token_usage.py:walk_jsonl_segment": (
        *_READ_ONLY,
        [
            "open(path, 'rb')",
        ],
    ),
    "token_usage.py:_extract_day": (
        *_READ_ONLY,
        [
            "raw_ts.replace('Z', '+00:00')",
            "dt.replace(tzinfo=timezone.utc)",
        ],
    ),
    "token_usage.py:head_fingerprint": (
        *_READ_ONLY,
        [
            "open(path, 'rb')",
        ],
    ),
    "upgrade.py:_install_is_running": (
        *_READ_ONLY,
        [
            "os.open(str(CACHE_DIR / INSTALL_LOCK_NAME), os.O_RDONLY)",
        ],
    ),
    "upgrade.py:_pipx_install_lock": (
        *_GUARDED,
        [
            "CACHE_DIR.mkdir(parents=True, exist_ok=True)",
            "os.open(str(CACHE_DIR / INSTALL_LOCK_NAME), os.O_CREAT | os.O_RDWR, 384)",
        ],
    ),
    "upgrade.py:_spawn_pipx": (
        *_GUARDED,
        [
            "log_path.parent.mkdir(parents=True, exist_ok=True)",
            "os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 384)",
        ],
    ),
    "upgrade.py:_write_update_log": (
        *_GUARDED,
        [
            "path.parent.mkdir(parents=True, exist_ok=True)",
            "os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 384)",
        ],
    ),
    "upgrade.py:_parse_iso": (
        *_READ_ONLY,
        [
            "dt.replace(tzinfo=timezone.utc)",
        ],
    ),
    "fsutil.py:_pytest_config_strings": (
        "read-only",
        "bounded real config read only",
        [
            "path.open('rb')",
        ],
    ),
}


def _check_inventory(sites, guards, reviewed=REVIEWED, *, write_flags=frozenset()):
    actual = Counter((owner, expression) for owner, expression, *_ in sites)
    expected = Counter(
        (owner, expression)
        for owner, (_, _, expressions) in reviewed.items()
        for expression in expressions
    )
    missing, stale = actual - expected, expected - actual
    assert not missing and not stale, (
        "Raw filesystem census changed. Add the fsutil guard before the first mutation "
        "and a refusal/control pair, then review the site classification. "
        "See docs/invariants/README.md#test-write-guard. "
        f"Unreviewed={dict(missing)!r}; removed/changed={dict(stale)!r}"
    )
    for owner, expression, line, primitive in sites:
        category, reason, _ = reviewed[owner]
        assert category in CATEGORIES and reason, owner
        if category == "read-only" and primitive == "open":
            assert owner not in write_flags, (
                f"{owner}: read-only open classification contains writable os flags; "
                "review this site"
            )
        if category == "guarded-here":
            # Read-only primitives may precede recovery guards in the same
            # function; those individual expressions get explicit exemptions.
            if (owner, expression) in READ_ONLY_BEFORE_GUARD:
                continue
            assert any(guard < line for guard in guards.get(owner, ())), (
                f"{owner}:{line} {expression}: "
                "guard must precede this primitive in the same function"
            )


READ_ONLY_BEFORE_GUARD = {
    ("seen_sources.py:read", "os.open(str(path), os.O_RDONLY)"),
}


def test_raw_mutation_sites_are_reviewed():
    sites, guards, write_flags = [], {}, set()
    for path in sorted(SRC.rglob("*.py")):
        found, guard_lines, writable = _inventory(path.read_text(), str(path.relative_to(SRC)))
        sites.extend(found)
        guards.update(guard_lines)
        write_flags.update(writable)
    _check_inventory(sites, guards, write_flags=write_flags)


@pytest.mark.parametrize(
    "source",
    [
        "def new_writer(path):\n    path.mkdir(parents=True)\n",
        "from os import unlink as erase\ndef new_writer(path):\n    erase(path)\n",
        "from builtins import open as file_open\ndef new_writer(path):\n"
        "    file_open(path, 'wb')\n",
        "def new_writer(path):\n    save = path.write_text\n    save('unsafe')\n",
        "import os as unix\ndef new_writer(path):\n    unix.remove(path)\n",
        "import os\ndef new_writer(path):\n    os.makedirs(path)\n",
        "def new_writer(path):\n    path.rmdir()\n",
        "def new_writer(path):\n    path.chmod(0o600)\n",
        "import os\ndef new_writer(path):\n    os.utime(path, None)\n",
        "import tempfile\ndef new_writer(path):\n    tempfile.mkdtemp(dir=path)\n",
        "import shutil as sh\ndef new_writer(src, dst):\n    sh.copyfile(src, dst)\n",
        "from shutil import rmtree as nuke\ndef new_writer(path):\n    nuke(path)\n",
        "import shutil\ndef new_writer(src, dst):\n    copy = shutil.copy2\n    copy(src, dst)\n",
        "import sqlite3\ndef new_writer(path):\n    sqlite3.connect(path)\n",
        "import shutil\ndef new_writer(path):\n    shutil.chown(path, 'nobody')\n",
        "from shutil import chown\ndef new_writer(path):\n    chown(path, 'nobody')\n",
        "import os\ndef new_writer(a, b):\n    os.renames(a, b)\n",
        "import shutil\ndef new_writer(path):\n    shutil.make_archive(path, 'zip')\n",
        "from shutil import unpack_archive\ndef new_writer(path):\n    unpack_archive(path)\n",
        "import tempfile\ndef new_writer(path):\n    tempfile.TemporaryFile(dir=path)\n",
    ],
)
def test_raw_mutation_census_detects_seeded_violation(source):
    sites, guards, _ = _inventory(source, "seeded.py")
    with pytest.raises(AssertionError, match="Unreviewed=.*seeded"):
        _check_inventory(sites, guards, {})


def test_raw_mutation_census_pins_order_and_duplicate_sites():
    source = "def writer(path):\n    path.mkdir()\n"
    sites, guards, _ = _inventory(source, "seeded.py")
    reviewed = {sites[0][0]: ("guarded-here", "seed", [sites[0][1]])}
    with pytest.raises(AssertionError, match="guard must precede"):
        _check_inventory(sites, guards, reviewed)
    source += "    fsutil.refuse_real_state_write_under_pytest(path)\n"
    sites, guards, _ = _inventory(source, "seeded.py")
    with pytest.raises(AssertionError, match="guard must precede"):
        _check_inventory(sites, guards, reviewed)
    source += "    path.mkdir()\n"
    sites, guards, _ = _inventory(source, "seeded.py")
    with pytest.raises(AssertionError, match="Unreviewed"):
        _check_inventory(sites, guards, reviewed)


def test_raw_mutation_census_rejects_writable_flags_in_read_only_site():
    for flag in ("O_RDONLY", "O_RDWR"):
        source = f"import os\ndef reader(path):\n    flags = os.{flag}\n    os.open(path, flags)\n"
        sites, guards, writable = _inventory(source, "seeded.py")
        reviewed = {sites[0][0]: ("read-only", "seed", [sites[0][1]])}
        if flag == "O_RDONLY":
            _check_inventory(sites, guards, reviewed, write_flags=writable)
        else:
            with pytest.raises(AssertionError, match="read-only open classification"):
                _check_inventory(sites, guards, reviewed, write_flags=writable)
