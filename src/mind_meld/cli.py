"""Mind Meld CLI — built with Typer.

Commands: init, push, pull, status, devices, diff, gc, autopull, autopush,
          sources, conflicts, resolve, retro-fleet, recapture.
"""

from __future__ import annotations

import copy
import difflib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import typer
from packaging.version import InvalidVersion, Version
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from mind_meld import (
    __version__,
    events,
    events_tail,
    fsutil,
    host_skill_discovery,
    identity,
    pullhistory,
    resolveflow,
    retention,
    seen_sources,
    sidecar,
    skill_link,
    upgrade,
)
from mind_meld import config as _config_module
from mind_meld.config import (
    DEFAULT_ARGON2_MEMORY_KB,
    DEFAULT_MAX_FILE_SIZE,
    DEFAULT_SOURCES,
    DEFAULT_STORAGE_PATH,
    MM_INTERNAL_SOURCE_NAMES,
    get_default_source,
    get_sources,
    grok_host_usage_enabled,
    load_config,
    patch_config_on_disk,
    save_config,
)
from mind_meld.conflictdiff import (
    count_divergent_lines,
    format_age_delta,
    render_banner,
    render_prompt,
    render_time_line,
    render_verdict,
)
from mind_meld.conflictmtime import (
    _bump_canonical_mtime_post_resolve,
    _restore_mtime_best_effort,
    _stat_mtime_btime,
)
from mind_meld.consoles import console, stderr_console
from mind_meld.crypto import (
    FORMAT_VERSION,
    CryptoInitFetch,
    bootstrap_crypto_init,
    decrypt,
    encrypt,
    fetch_crypto_init,
    get_passphrase,
    load_master_key,
    root_salt_fingerprint,
    set_crypto_session,
    store_passphrase_in_keyring,
    verify_passphrase,
)
from mind_meld.devices import (
    _list_devices_impl,
    generate_unique_short_device_id,
    list_devices,
    list_devices_with_drops,
    lookup_device_by_short_id,
    register_device,
    update_last_seen,
)
from mind_meld.errors import (
    ConfigError,
    CryptoError,
    LockError,
    ManifestError,
    MindMeldError,
    StorageError,
)
from mind_meld.lockedjson import locked_json_rmw, locked_json_snapshot
from mind_meld.lockfile import acquire_lock, release_lock
from mind_meld.manifest import (
    CONFLICT_INFIX,
    CONFLICT_V1_MARKER,
    MARKER_SKIP_NAME,
    DiffResult,
    _under_skip_prefix,
    build_manifest_v2,
    collect_tombstones,
    deserialize_manifest,
    diff_files,
    generate_tombstones,
    hash_file,
    is_conflict_filename,
    is_pre_inversion_conflict_filename,
    is_tombstoned,
    load_manifest,
    marker_skip_globs,
    mtime_from_manifest,
    mtime_from_path,
    parse_conflict_created_at,
    parse_conflict_device_short,
    read_and_hash,
    serialize_manifest,
    walk_source,
)
from mind_meld.manifest import (
    _is_excluded as _manifest_is_excluded,
)
from mind_meld.merge import lcs_merge, merge_file, should_merge
from mind_meld.retention import CONFLICT_AGE_DAYS
from mind_meld.safety import (  # noqa: F401 — re-exported for backwards-compat
    safe_str,
    safe_text,
    strip_terminal_escapes,
)
from mind_meld.storage import get_backend
from mind_meld.storage.keys import (
    DATA_PREFIX,
    DEVICES_PREFIX,
    MANIFESTS_PREFIX,
    blob_key,
    device_key,
    manifest_key,
    parse_blob_key,
)
from mind_meld.storage.local import LocalBackend
from mind_meld.synclog import write_sync_log

ApplyOutcome = Literal[
    "written",
    "merged",
    "merged-via-lcs",
    "skipped",
    "conflicted",
    "unchanged",
    "failed",
]
# Track 12A: the eligibility invariant for the deferred keep-canonical bump,
# centralized at the _download_and_apply seam. A path is drain-eligible iff its
# last outcome was the keep-canonical "skipped" (the RECORD case). Any outcome
# in this set means a canonical-mutating decision (write, merge, sidecar) hit
# disk successfully, so an earlier pending bump for that path is void — pop it.
# Success-only by construction: _apply_incoming_file returns these ONLY on
# successful canonical mutation (write/merge/sidecar); on failure it returns
# "failed", which is intentionally absent here so the prior decision stands.
# "skipped" / "unchanged" leave canonical untouched, so the prior decision
# also stands.
_CANONICAL_WRITE_OUTCOMES: frozenset[ApplyOutcome] = frozenset(
    {"written", "merged", "merged-via-lcs", "conflicted"}
)
FetchStatus = Literal["ok", "missing", "corrupt"]


# Track 5E (v0.9.2 BREAKING): minimum peer version required for safe pull.
# v0.9.2 inverted the conflict-direction semantics — a peer running an
# older mm would produce conflict files under the OLD direction (canonical
# = remote, sidecar = local), but a v0.9.2 puller dispatches by filename
# prefix (`v0-` = pre-inversion, no prefix = post-inversion) and would
# silently mis-resolve the peer's just-produced files. Refuse the pull
# until every peer reports last_seen_version >= this constant.
INVERSION_MIN_VERSION = "0.9.2"


@dataclass
class ManifestFetch:
    """Tri-state result of fetching a device's remote manifest.

    "ok"      — at least one copy decrypted successfully; `manifest` is set.
    "missing" — no manifest exists at the expected key (first push, or device
                never pushed). `manifest` is None. Callers should treat this
                as "no prior state" — e.g. push with empty tombstones.
    "corrupt" — manifest(s) exist but every copy failed to decrypt/parse.
                `manifest` is None. Callers that read state (status, diff,
                pull) should surface this to the user; callers that WRITE
                state (push, gc) must NOT silently treat this as missing
                because doing so drops tombstones / orphans blobs.
    """

    status: FetchStatus
    manifest: dict | None = None

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


@dataclass
class PullResult:
    """Result of a pull operation.

    Outcomes are split per the _apply_incoming_file decision tree:
      written    — local had no copy; remote written to canonical path
      merged     — .jsonl or MEMORY.md union-merged with local
      skipped    — local is newer than remote (no write)
      conflicted — hashes differed and remote newer/equal: local renamed to
                   .sync-conflict-*, remote written to canonical
      unchanged  — apply-time re-read showed local already matches remote
      failed     — rename/write error; local preserved, no change applied

    `total_skipped_unknown_source` counts (device, source_name) pairs where
    a peer advertised a source name the local config doesn't know about
    (partition risk: if a user renames a source, peers' data stops syncing
    silently). One increment per (device, source) pair per pull, not per file.
    """

    total_written: int = 0
    total_merged: int = 0
    total_skipped: int = 0
    total_conflicted: int = 0
    total_failed: int = 0
    total_skipped_unknown_source: int = 0
    bytes_transferred: int = 0
    device_names: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    # Degradation signals: set to non-zero when the pull succeeded overall
    # but some part of it is at-risk. autopull() aggregates these into the
    # `degraded` breadcrumb outcome. Counts only — detailed failure records
    # live in fsync_warnings / corrupt_peers inside _pull_core and are
    # surfaced to stderr by _print_pull_summary.
    durability_fsync_failures: int = 0
    corrupt_peer_count: int = 0

    @property
    def total_applied(self) -> int:
        """Files actually changed on disk (written + merged + conflicted)."""
        return self.total_written + self.total_merged + self.total_conflicted


@dataclass
class PushResult:
    """Result of a push operation."""

    total_new: int = 0
    total_modified: int = 0
    total_deleted: int = 0
    bytes_transferred: int = 0
    elapsed: float = 0.0
    # Events-tail degradations for this push, one human-readable phrase each
    # (v0.12.16). Empty on a healthy push. `autopush` turns a non-empty list
    # into a `degraded` autorun breadcrumb so `mm status` stops reporting
    # `success` while the retro pipeline is dead — the events tail is
    # forensic-only and swallows its own failures, so before this the ONLY
    # signal was a stderr notice from a command that runs unattended from a
    # Claude Code hook, i.e. nobody's terminal. Mirrors the `degradations`
    # list `autopull` has carried since v0.8.1.
    events_degradations: list[str] = field(default_factory=list)


app = typer.Typer(
    name="mm",
    help="Mind Meld — sync Claude Code sessions and other sources across machines.",
    add_completion=True,
)
# `console` / `stderr_console` now live in `mind_meld.consoles` (Track 16A) so
# `resolveflow` and `retention` can render through the SAME objects without
# importing `cli` — see that module's docstring for why sharing the instances is
# load-bearing. Imported into this namespace so the ~280 existing bare
# `console.print(...)` call sites stay unchanged.
#
# stderr_console is the dedicated stderr sink for _error and other failure-path
# output. Rich formatting is preserved in interactive terminals; pipes
# (autopush/autopull quiet mode, CI) get a clean stdout and one-line stderr per
# the contract documented in README.md "Claude Code Integration." A single
# module-level instance rather than ad-hoc construction keeps color-capability
# detection and terminal-width behavior consistent across call sites.


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mm {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
    no_check_version: bool = typer.Option(
        False,
        "--no-check-version",
        help=(
            "Skip the auto-upgrade nudge for this invocation. "
            "Force-skips regardless of \\[upgrade] auto_check in config."
        ),
    ),
) -> None:
    # Wire the per-invocation override into the upgrade module before any
    # command body runs. Force-skips both `check_for_upgrade` and
    # `detect_self_version_transition` for this process.
    if no_check_version:
        upgrade.set_invocation_skip(True)


def _error(msg: str) -> None:
    # Route to stderr so quiet-mode autopush/autopull don't leak error text
    # to stdout (violating the one-line-stderr contract). Interactive users
    # still see the [red]Error:[/red] formatting because terminals render
    # stderr alongside stdout — the separation only matters when stdout is
    # being consumed programmatically.
    stderr_console.print(f"[red]Error:[/red] {safe_str(msg)}")
    raise typer.Exit(1)


def _list_devices_warn(backend: LocalBackend) -> list[dict]:
    """list_devices variant that surfaces dropped entries to stderr.

    Loss of a peer's device entry is load-bearing during corrupt-manifest
    recovery: the peer's tombstones are unreachable without its device_id,
    so silent drops can mask a recoverable manifest (see TODOS.md
    "Blob-directory as secondary peer-discovery path"). Emitting a warning
    per dropped entry at least makes the gap visible to support triage.

    Library callers (and direct tests) should continue to call
    `devices.list_devices` — that variant is intentionally silent so
    programmatic consumers don't spam stderr.
    """

    def _warn(key: str, reason: str) -> None:
        stderr_console.print(
            f"[yellow]Warning:[/yellow] dropped device entry {safe_str(key)} — {safe_str(reason)}"
        )

    return _list_devices_impl(backend, on_drop=_warn)


def _get_config() -> dict:
    try:
        config = load_config()
    except MindMeldError as e:
        _error(str(e))
        raise  # unreachable, but keeps type checker happy
    # Seam 1 — transition detection. Idempotent within a process; safe to
    # call after every successful load_config. Codex outside voice (D6)
    # rejected refactoring _auto_command_setup / init_cmd through this
    # function (would break their distinct error policies); both call
    # `upgrade.run_transition_hook` directly instead.
    upgrade.run_transition_hook(config)
    return config


def _get_passphrase_or_exit() -> str:
    try:
        return get_passphrase()
    except CryptoError as e:
        _error(str(e))
        raise
    except Exception as e:
        # crypto.get_passphrase narrowed its keyring catch to
        # (KeyringError, ImportError) in v0.8.9. Other exception kinds
        # (OSError, RuntimeError from a misbehaving backend) now
        # propagate. Route them through _error() so interactive commands
        # print the one-line red banner instead of an uncaught traceback —
        # break-glass path (MINDMELD_PASSPHRASE env var) is already past,
        # so the user needs to see exactly why the keyring hiccuped.
        _error(f"keyring backend failure: {type(e).__name__}: {e}")
        raise


def _init_crypto_session(backend: LocalBackend, passphrase: str, config: dict) -> int:
    """Read mm-crypto-init, drift-check against local config, pin process crypto session.

    Called at the top of every crypto-using command (push/pull/status/diff/gc/etc).
    Returns the authoritative argon2_memory_kb from storage — callers should use
    THIS value, not config["crypto"]["argon2_memory_kb"], to avoid silent drift
    between local config and storage.

    Side-effect: if local config's root_salt_fp is missing (e.g. first command
    after init), populate and save it so future commands can drift-check.

    Raises:
      MindMeldError subclasses — caller chooses presentation (_error for
      interactive; stderr print for autopull/autopush).
    """
    fetch = fetch_crypto_init(backend)
    if fetch.status == "missing":
        raise CryptoError(
            "crypto: mm-crypto-init not found at storage root. "
            "Run 'mm init' to initialize this storage."
        )
    if fetch.status == "corrupt":
        raise CryptoError(
            "crypto: mm-crypto-init is corrupt. If another device still has a "
            "valid copy in its local iCloud cache, wait for sync to reconcile. "
            "Otherwise delete mm-crypto-init from the storage root and re-run "
            "'mm init' (WARNING: destroys all existing v2 blobs for every device)."
        )

    assert fetch.root_salt is not None and fetch.argon2_memory_kb is not None
    storage_fp = root_salt_fingerprint(fetch.root_salt)
    local_fp = config.get("crypto", {}).get("root_salt_fp")

    if local_fp and local_fp != storage_fp:
        raise CryptoError(
            f"crypto: mm-crypto-init root_salt changed since this device was "
            f"initialized (local fp={local_fp}, storage fp={storage_fp}). "
            f"Another device may have bootstrapped storage concurrently. "
            f"Re-run 'mm init' to reconfigure against the current storage."
        )

    set_crypto_session(fetch.root_salt, fetch.argon2_memory_kb)
    master_key = load_master_key(passphrase, fetch.root_salt, fetch.argon2_memory_kb)
    assert fetch.keycheck_blob is not None
    verify_passphrase(master_key, fetch.keycheck_blob)

    # Backfill local config if needed (first command after an upgrade or
    # a previously-uninitialized config). Silent one-time write.
    #
    # In-memory update serves this process's drift-check on later calls.
    # Persist via patch_config_on_disk (re-reads raw TOML and merges only
    # the crypto keys) so `_apply_defaults`' in-memory path canonicalization
    # is NOT written back over the user's hand-edited paths. Without this,
    # a user's `storage.path = "~/Library/..."` silently becomes the resolved
    # absolute form on the first run after upgrade, and symlinks get
    # dereferenced. See ROADMAP.md Track 2B / CLAUDE.md for the contract.
    if not local_fp:
        crypto_patch = {
            "root_salt_fp": storage_fp,
            "argon2_memory_kb": fetch.argon2_memory_kb,
        }
        config.setdefault("crypto", {}).update(crypto_patch)
        try:
            patch_config_on_disk({"crypto": crypto_patch})
        except OSError:
            pass  # non-fatal; drift check just won't fire next run
        except ConfigError as e:
            # ConfigError here signals on-disk TOML became malformed between
            # load_config and this call (concurrent editor crash, disk issue).
            # Per CLAUDE.md v0.8.1 visible-failure contract: data-at-risk
            # signals reach stderr even in quiet mode.
            print(f"mm: warning: backfill skipped — {e}", file=sys.stderr)

    return fetch.argon2_memory_kb


def _make_manifest_validator(passphrase: str, memory_kb: int) -> Callable[[Path], bool]:
    """Return a predicate that accepts `path` only if it decrypts AND
    deserializes as a Mind Meld manifest.

    Used by find_conflict_copies / delete_conflict_copies in storage/local.py
    to reject bogus siblings (random files whose name happens to match the
    iCloud or Dropbox rename pattern). Without this, `_fetch_remote_manifest`
    can spuriously flip status=missing → status=corrupt when the user drops
    a stray file into manifests/<device>/.

    Catches the same exception set as _fetch_remote_manifest's inline
    decrypt/deserialize (see cli.py:197) so the definitions of "valid" agree.
    OSError is included to cover TOCTOU (the file disappears mid-read because
    iCloud sync removed it) — the candidate is simply treated as "not a real
    conflict" rather than crashing the caller.
    """

    def is_valid(path: Path) -> bool:
        try:
            enc_data = path.read_bytes()
        except OSError:
            # File vanished mid-scan (iCloud sync race). Treat as "not a
            # conflict" — caller skips it, next fetch will re-evaluate.
            return False
        # Cheap magic-byte shortcut before paying Argon2. Non-Mind-Meld
        # files (stray backups, user scratch files) bail out in ~1ms
        # instead of ~200-500ms per candidate.
        if not enc_data or enc_data[0] != FORMAT_VERSION:
            return False
        try:
            plain = decrypt(enc_data, passphrase, memory_kb)
            deserialize_manifest(plain)
            return True
        except Exception:
            # Defense in depth: a single malformed candidate (e.g., stale
            # passphrase after `mm init`, corrupt ciphertext, unexpected
            # argon2 error) must never crash the whole recovery sweep.
            return False

    return is_valid


def _fetch_remote_manifest(
    backend: LocalBackend, device_id: str, passphrase: str, memory_kb: int
) -> ManifestFetch:
    """Fetch and decrypt remote manifest, merging any conflict copies.

    Read-only: does NOT delete conflict copies. Use _cleanup_conflict_copies()
    after the manifest has been successfully used in a mutating operation.

    Tri-state return — see ManifestFetch. Callers MUST distinguish MISSING
    (no manifest at all — valid first-push state) from CORRUPT (manifest(s)
    exist but unreadable — needs recovery path). Conflating the two drops
    tombstones on every first push.

    Returns a ManifestFetch whose `manifest` (when status="ok") is already
    normalized via load_manifest — `sources` and `tombstones` keys are
    guaranteed dicts with v2 shape. Callers may rely on this contract and
    do not need to call normalize_manifest themselves.
    """
    mkey = manifest_key(device_id)
    manifests: list[dict] = []

    is_valid_manifest = _make_manifest_validator(passphrase, memory_kb)
    canonical_exists = backend.exists(mkey)
    conflict_copies = backend.find_conflict_copies(mkey, is_valid_manifest)
    had_any_source = canonical_exists or bool(conflict_copies)

    # Try canonical manifest. Storage-layer errors (OSError, StorageError, any
    # MindMeldError) are treated as "this copy unreadable" — try conflict
    # copies next. Without this, a TOCTOU race between backend.exists() and
    # backend.get(), or a permission flip mid-scan, would escape as an
    # exception and crash recovery.
    if canonical_exists:
        try:
            enc_data = backend.get(mkey)
            plain = decrypt(enc_data, passphrase, memory_kb)
            manifests.append(load_manifest(plain))
        except (CryptoError, ManifestError, OSError, MindMeldError):
            pass  # canonical unreadable — try conflict copies

    # Try conflict copies (iCloud/Dropbox)
    for conflict_path in conflict_copies:
        try:
            enc_data = conflict_path.read_bytes()
            plain = decrypt(enc_data, passphrase, memory_kb)
            manifests.append(load_manifest(plain))
        except (CryptoError, ManifestError, OSError, MindMeldError):
            pass  # skip unreadable conflict copies

    if not manifests:
        if had_any_source:
            return ManifestFetch(status="corrupt")
        return ManifestFetch(status="missing")

    if len(manifests) == 1:
        return ManifestFetch(status="ok", manifest=manifests[0])

    # Merge multiple manifests additively.
    #
    # INVARIANT (load-bearing): files are merged as a UNION across conflict
    # copies (see _merge_manifests). Tombstones are newest-timestamp-wins.
    # The asymmetry is correct because the manifest walker is LOSSY — it
    # drops files on permission errors, size-exceeded, and read failures
    # (manifest.py:walk_claude_source, walk_generic_source). A file missing
    # from the newer conflict copy is NOT causal evidence of deletion; only
    # an explicit tombstone is. Swapping files to newest-wins would silently
    # resurrect-via-erase any file that happened to be locked/unreadable
    # during one scan but not another. The correctness guarantee is:
    # union-for-files + newest-wins-for-tombstones + is_tombstoned() gate
    # at every downstream consumer. See SPEC.md "Merge invariants".
    return ManifestFetch(status="ok", manifest=_merge_manifests(manifests))


def _build_exclude_map(
    config: dict,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Map source name -> exclude_patterns and marker-skip prefixes.

    Empty glob entries are pruned. Marker-skip prefixes (directories
    containing `.extend-root`) are returned separately so the consumer-
    boundary filter can prefix-match rather than fnmatch. Without that
    merge, a walker-only skip would emit deletion tombstones for
    previously-synced generated files.
    """
    out: dict[str, list[str]] = {}
    prefixes: dict[str, list[str]] = {}
    for src in get_sources(config):
        patterns = list(src.get("exclude_patterns") or [])
        prefs = marker_skip_globs(src)
        if patterns:
            out[src["name"]] = patterns
        if prefs:
            prefixes[src["name"]] = prefs
    return out, prefixes


def _filter_excluded_paths(
    manifest: dict,
    exclude_map: dict[str, list[str]],
    skip_prefixes: dict[str, list[str]] | None = None,
) -> dict:
    """Return a shallow copy of `manifest` with excluded source-paths stripped.

    Drops entries from `sources.<name>.files` and `tombstones` whose relative
    path matches any glob in `exclude_map[name]`, AND any conflict-shaped
    basename (mirroring the push-side ``manifest._is_excluded`` gate). A
    peer-chosen ``foo.sync-conflict-19700101-000000-deadbeef.md`` must not
    materialize — after the gc bar reads the filename timestamp, that
    string would drive the reap age directly.

    Empty `exclude_map` still strips conflict-shaped names; if none are
    present it returns `manifest` unchanged (load-bearing for hot paths:
    avoid copying large manifests when no source declares excludes).

    Apply at the CONSUMER boundary (_pull_core, _push_core), NOT inside
    `_fetch_remote_manifest`. `mm gc` walks raw manifests via
    `_fetch_remote_manifest` to compute referenced blobs — a filtered manifest
    there would mark live peer blobs as orphans and silently delete them
    (codex-2 #1, the gc-bypass hazard). The filter is a CONSUMER-side fix
    for the tombstone-on-exclude-transition bug, not a fetch-side scrub.

    Tombstone keys are `<source>:<rel_path>` post-Track-1B. Bare-path keys
    (legacy v1 manifests) default to the `claude` source per the same
    promotion rule `normalize_manifest` applies.
    """

    def _conflict_shaped(rel_path: str) -> bool:
        filename = rel_path.rsplit("/", 1)[-1]
        return is_conflict_filename(filename) or filename == MARKER_SKIP_NAME

    def _excluded(src_name: str, rel_path: str) -> bool:
        if _conflict_shaped(rel_path):
            return True
        if _under_skip_prefix(rel_path, (skip_prefixes or {}).get(src_name)):
            return True
        patterns = exclude_map.get(src_name)
        if not patterns:
            return False
        return any(fnmatch.fnmatch(rel_path, p) for p in patterns)

    if not exclude_map and not skip_prefixes:
        has_conflict = False
        for src_data in manifest.get("sources", {}).values():
            for rel in src_data.get("files", {}):
                if _conflict_shaped(rel):
                    has_conflict = True
                    break
            if has_conflict:
                break
        if not has_conflict:
            for key in manifest.get("tombstones", {}):
                if isinstance(key, str) and ":" in key:
                    _, _, rel = key.partition(":")
                else:
                    rel = key
                if isinstance(rel, str) and _conflict_shaped(rel):
                    has_conflict = True
                    break
        if not has_conflict:
            return manifest

    out = dict(manifest)

    new_sources: dict[str, dict] = {}
    for src_name, src_data in manifest.get("sources", {}).items():
        files = src_data.get("files", {})
        new_files = {rel: info for rel, info in files.items() if not _excluded(src_name, rel)}
        if new_files == files:
            new_sources[src_name] = src_data
        else:
            new_sources[src_name] = {**src_data, "files": new_files}
    out["sources"] = new_sources

    new_tombstones: dict[str, dict] = {}
    for key, info in manifest.get("tombstones", {}).items():
        if isinstance(key, str) and ":" in key:
            src_name, rel = key.split(":", 1)
        else:
            src_name, rel = "claude", key  # legacy bare-path tombstones
        if _excluded(src_name, rel):
            continue
        new_tombstones[key] = info
    out["tombstones"] = new_tombstones

    return out


def _has_symlinked_component(path: Path, base_path: Path) -> bool:
    """Whether ``path`` traverses a symlink below its source root.

    A symlinked source root is legitimate: it is the user's chosen location
    for the whole source. Any link below that root is local routing and must
    neither be published nor followed while applying a peer's bytes.
    """
    try:
        relative = path.relative_to(base_path)
    except ValueError:
        return True

    component = base_path
    for part in relative.parts:
        component /= part
        if component.is_symlink():
            return True
    return False


def _filter_symlinked_paths(manifest: dict, sources: list[dict[str, Any]]) -> dict:
    """Remove locally-symlinked paths from a prior manifest before tombstones.

    ``walk_generic_source`` deliberately omits symlinks. Filtering the prior
    manifest at this consumer boundary prevents that omission from minting a
    fleet-wide deletion tombstone, including for pre-migration explicit source
    configurations that do not yet carry the new default exclude globs.
    """
    source_roots = {
        source["name"]: Path(source["path"]).expanduser()
        for source in sources
        if source.get("type") == "generic" and source.get("name") and source.get("path")
    }
    if not source_roots:
        return manifest

    def _symlinked(source_name: str, rel_path: str) -> bool:
        base_path = source_roots.get(source_name)
        return base_path is not None and _has_symlinked_component(base_path / rel_path, base_path)

    out = dict(manifest)
    out["sources"] = {
        source_name: {
            **source_data,
            "files": {
                rel_path: info
                for rel_path, info in source_data.get("files", {}).items()
                if not _symlinked(source_name, rel_path)
            },
        }
        for source_name, source_data in manifest.get("sources", {}).items()
    }

    out["tombstones"] = {
        key: info
        for key, info in manifest.get("tombstones", {}).items()
        if not (isinstance(key, str) and ":" in key and _symlinked(*key.split(":", 1)))
    }
    return out


def _filter_disabled_sources(manifest: dict, disabled: list[str]) -> dict:
    """Return a shallow copy of `manifest` with disabled-source entries stripped.

    Drops `sources.<name>` entries whose source name is in `disabled`.
    Empty `disabled` returns `manifest` unchanged (load-bearing for hot paths).

    Apply at the CONSUMER boundary (`_pull_core`, `_push_core`), NOT inside
    `_fetch_remote_manifest`. Same hazard as `_filter_excluded_paths`:
    `mm gc` reads raw manifests via that path to compute referenced blobs,
    and a filtered manifest there would mark live peer blobs as orphans.
    Mirror of the exclude_patterns invariant (CLAUDE.md, 2026-04-24
    first-pull regression).

    Without this filter at `_push_core`: disabling a source on machine A
    and pushing would generate a deletion tombstone for every file in that
    source (because `generate_tombstones` compares prior_manifest with
    new_manifest, and the new manifest no longer has the disabled source
    via get_sources's filter). Spurious tombstones suppress restoration
    and propagation of a missing path across upgraded and stale peers
    until expiry (`manifest.TOMBSTONE_TTL_DAYS = 30`, re-broadcast
    newest-wins by `collect_tombstones`). Existing local bytes are never
    removed. The consumer-boundary filter is still required: a 30-day
    fleet-wide propagation freeze is the failure it prevents.

    **Asymmetric filter — sources stripped, tombstones preserved.** Codex
    adversarial review 2026-04-25 caught this: if A deletes `gstack:x`,
    pushes a real tombstone, then disables gstack and pushes again, dropping
    the prior tombstone purges A's legitimate deletion. A long-offline peer
    that comes back and pulls only A's manifest would lose the deletion and
    `x` would resurrect. Tombstones encode prior consensus that disabling
    must not undo. Generate_tombstones still won't ADD spurious tombstones
    because both prior and new manifest lack the disabled source's `sources`
    section — empty diff = no new tombstones. Existing
    `tombstones[<disabled>:*]` keys flow through unchanged.
    """
    if not disabled:
        return manifest

    disabled_set = set(disabled)
    out = dict(manifest)

    out["sources"] = {
        name: data for name, data in manifest.get("sources", {}).items() if name not in disabled_set
    }
    # Tombstones intentionally preserved across the disable filter — see
    # the asymmetric-filter rationale in the docstring above.

    return out


def _detect_case_insensitive_fs(path: Path) -> bool:
    """Probe whether `path`'s volume is case-insensitive (APFS default, NTFS).

    Non-invasive: no writes. Constructs a swapcase variant of the path's
    own basename and checks via `samefile()` whether both names resolve
    to the same inode. Returns False on any failure (safer default — no
    spurious case-collision warnings on Linux ext4).

    Skips paths whose basename has no alphabetic characters (can't be
    case-mangled meaningfully). Skips when the swapcase produces the same
    name (basename was already case-neutral).
    """
    if not path.exists():
        return False
    name = path.name
    if not any(c.isalpha() for c in name):
        return False
    alt_name = name.swapcase()
    if alt_name == name:
        return False
    alt = path.parent / alt_name
    try:
        return alt.exists() and alt.samefile(path)
    except OSError:
        return False


def _detect_pull_case_collisions(
    manifest_cache: dict[str, dict | None],
    local_sources_map: dict[str, dict[str, Any]],
) -> dict[str, dict[str, list[str]]]:
    """Detect case-collision clusters in peer manifest paths per source.

    Returns `{src_name: {casefold_key: [colliding_rel_paths_sorted]}}`
    only for sources whose local base_path is on a case-insensitive FS
    AND have ≥2 distinct rel_paths casefolding to the same key. Empty
    dict on case-sensitive volumes — no local collision possible.

    Aggregates across ALL peer manifests so a collision between peer A's
    "Projects/x.md" and peer B's "projects/x.md" is detected even when
    neither peer alone exposes both casings.
    """
    collisions: dict[str, dict[str, list[str]]] = {}
    for src_name, src_info in local_sources_map.items():
        base_path = src_info["path"]
        if not _detect_case_insensitive_fs(base_path):
            continue
        seen_paths_by_key: dict[str, set[str]] = {}
        for peer_manifest in manifest_cache.values():
            if peer_manifest is None:
                continue
            src_data = peer_manifest.get("sources", {}).get(src_name)
            if not src_data:
                continue
            for rel_path in src_data.get("files", {}):
                key = rel_path.casefold()
                seen_paths_by_key.setdefault(key, set()).add(rel_path)
        clusters = {
            key: sorted(paths) for key, paths in seen_paths_by_key.items() if len(paths) > 1
        }
        if clusters:
            collisions[src_name] = clusters
    return collisions


def _drop_case_collisions_from_manifests(
    manifest_cache: dict[str, dict | None],
    collisions: dict[str, dict[str, list[str]]],
) -> dict[str, dict | None]:
    """Drop all-but-lex-first colliding rel_paths from each peer manifest.

    Returns a new cache; input is not mutated. Caller emits one
    `mm: warning:` per cluster naming the kept and dropped paths
    (visible-failure contract). Tombstones are not touched — collision
    is about per-pull WRITES on case-insensitive consumer; tombstones
    encode prior consensus and stay intact (mirrors the asymmetric
    `_filter_disabled_sources` invariant).
    """
    if not collisions:
        return manifest_cache
    new_cache: dict[str, dict | None] = {}
    for did, manifest in manifest_cache.items():
        if manifest is None:
            new_cache[did] = None
            continue
        new_manifest = copy.deepcopy(manifest)
        for src_name, clusters in collisions.items():
            src_data = new_manifest.get("sources", {}).get(src_name)
            if not src_data:
                continue
            files = src_data.get("files", {})
            for paths in clusters.values():
                for drop in paths[1:]:
                    files.pop(drop, None)
        new_cache[did] = new_manifest
    return new_cache


def _recover_prior_manifest(
    fetch: ManifestFetch,
    backend: LocalBackend,
    device_id: str,
    passphrase: str,
    memory_kb: int,
    *,
    quiet: bool = False,
) -> dict | None:
    """Resolve a prior-state manifest for tombstone generation.

    Called by _push_core after fetching THIS device's remote manifest.
    Returns the manifest to pass to `generate_tombstones` as `remote_manifest`.

    Recovery chain:
      1. status == "ok"       → return the fetched manifest (normal path).
      2. status == "missing"  → return None (first push; no prior state is
                                 the correct answer, tombstones will be empty).
      3. status == "corrupt"  → try sidecar (fresh-deletion-preserving);
                                 then peer fallback (propagated tombstones
                                 only); then _error with recovery steps.

    Raises typer.Exit(1) via _error() when corrupt + no sidecar + no peers.
    """
    if fetch.is_ok:
        return fetch.manifest
    if fetch.status == "missing":
        return None

    # status == "corrupt" — recovery chain
    sidecar_manifest = sidecar.read(device_id)
    if sidecar_manifest is not None:
        msg = (
            "remote manifest corrupt; recovered prior state from local "
            f"sidecar ({sidecar.sidecar_path()})."
        )
        if quiet:
            # Load-bearing: silently swallowing a corrupt-manifest recovery
            # in autopush leaves the user with no signal that storage is
            # degrading. Always surface to stderr.
            print(f"mm: warning: {msg}", file=sys.stderr)
        else:
            console.print(f"[yellow]Warning:[/yellow] {msg}")
        return sidecar_manifest

    # No sidecar — try peer fallback
    peer_tombstones = _collect_peer_tombstones(backend, device_id, passphrase, memory_kb)
    if peer_tombstones:
        msg = (
            "remote manifest corrupt and no local sidecar; recovered "
            f"{len(peer_tombstones)} tombstone(s) from peer device(s). "
            "Recent local deletions may be lost — verify no files have "
            "resurrected."
        )
        if quiet:
            # Load-bearing: peer-fallback recovery is the riskiest branch
            # (recent deletions can be lost). Must reach the user even in
            # autopush.
            print(f"mm: warning: {msg}", file=sys.stderr)
        else:
            console.print(f"[yellow]Warning:[/yellow] {msg}")
        # Synthetic prior manifest: no prior file list (so fresh-deletion
        # detection is disabled — we have no basis for it), but the
        # carry-forward loop in generate_tombstones() preserves these.
        return {"sources": {}, "tombstones": peer_tombstones}

    # Nothing to recover from — refuse rather than silently drop tombstones.
    _error(
        "remote manifest corrupt, no local sidecar, and no peer manifests "
        "available for recovery. Run 'mm status' to inspect storage state, "
        "then 'mm init' if storage is unrecoverable. Pushing now would "
        "erase this device's deletion records across your fleet."
    )
    return None  # unreachable; _error raises


def _collect_peer_tombstones(
    backend: LocalBackend, my_device_id: str, passphrase: str, memory_kb: int
) -> dict[str, dict[str, str]]:
    """Aggregate tombstones from all peer devices. Returns {} if none.

    `list_devices` can fail if the storage layer raises (permissions, I/O) or
    if the devices-directory read itself errors. We swallow those specifically
    so recovery can fall through to "refuse" rather than crashing mid-recovery.
    Unexpected exceptions propagate — we don't want to mask bugs here.
    """
    try:
        devices = _list_devices_warn(backend)
    except (OSError, MindMeldError):
        return {}

    peer_manifests: dict[str, dict | None] = {}
    for d in devices:
        did = d["device_id"]
        if did == my_device_id:
            continue
        # Per-peer try/except — one flaky peer must not abort recovery.
        try:
            peer_fetch = _fetch_remote_manifest(backend, did, passphrase, memory_kb)
            peer_manifests[did] = peer_fetch.manifest if peer_fetch.is_ok else None
        except (OSError, MindMeldError):
            peer_manifests[did] = None

    if not peer_manifests:
        return {}

    return collect_tombstones(
        list(peer_manifests.keys()),
        lambda did: peer_manifests.get(did),
    )


def _manifest_content_hash(manifest: dict) -> str:
    """Stable SHA-256 of a manifest's canonical JSON.

    Used as the deterministic tiebreaker in `_merge_manifests` when two
    conflict copies carry identical ISO-second timestamps. Canonical JSON
    means sorted keys + no ASCII escaping, so the hash is stable across
    any platform that implements the same JSON serializer contract.
    """
    canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _merge_manifests(manifests: list[dict]) -> dict:
    """Merge multiple manifest variants additively.

    For each source, takes the union of all files. When the same relative path
    appears in multiple manifests, the entry from the manifest with the latest
    timestamp wins.

    Policy asymmetry (load-bearing): files are UNIONED across copies, tombstones
    are newest-timestamp-wins. The walker is lossy (permission errors, size
    caps, read failures drop files from a scan), so absence from a newer copy
    is NOT causal evidence of deletion — only tombstones are. Pairing this with
    the `is_tombstoned()` gate at every consumer of a merged manifest is what
    keeps deletions correct. See SPEC.md "Merge invariants".

    Merge order (N conflict copies for ONE device):

        timestamps differ:          [t1] [t2] [t3]  →  sort by t        →  [t1, t2, t3]
                                                                                  └─ base (winner)

        timestamps tie (t2a == t2b): [t2a] [t2b]    →  sort by (t, hash) →  determined by
                                                                            lex-ordering of
                                                                            content hashes:
                                                                            stable across devices
                                                                            regardless of FS
                                                                            listing order from
                                                                            find_conflict_copies.

    `device_id` is NOT in the sort key: every input here is a conflict copy
    of the SAME device's manifest (cli.py:_fetch_remote_manifest keys the
    fetch by device_id), so device_id is constant across inputs and would
    be a no-op tiebreaker.
    """
    # Sort by (timestamp, content_hash) so same-second conflict copies produce
    # a deterministic base across devices. Without the content-hash tiebreak,
    # Python's stable sort preserves find_conflict_copies insertion order,
    # which comes from Path.glob (filesystem-dependent, not sorted cross-device).
    sorted_manifests = sorted(
        manifests,
        key=lambda m: (m.get("timestamp", ""), _manifest_content_hash(m)),
    )

    merged = dict(sorted_manifests[-1])  # start with latest as base
    merged_sources: dict[str, dict] = {}

    # Inputs are pre-normalized via load_manifest at _fetch_remote_manifest;
    # we may rely on `sources` and `tombstones` being present + v2-shaped.
    for m in sorted_manifests:
        for src_name, src_data in m.get("sources", {}).items():
            if src_name not in merged_sources:
                merged_sources[src_name] = {
                    "base_path": src_data.get("base_path", ""),
                    "files": {},
                }
            # Union: later manifests overwrite earlier for same path
            merged_sources[src_name]["files"].update(src_data.get("files", {}))

    merged["sources"] = merged_sources
    # Track 1B: the redundant top-level "files" mirror is not written by
    # this code (and `normalize_manifest` strips it on v2 passthrough), so
    # `merged` inherits whatever came via `dict(sorted_manifests[-1])`
    # above — which is always files-free post-normalize.

    # Merge tombstones additively too
    merged_tombstones: dict[str, dict] = {}
    for m in sorted_manifests:
        for path, info in m.get("tombstones", {}).items():
            existing = merged_tombstones.get(path)
            if existing is None or info.get("deleted_at", "") > existing.get("deleted_at", ""):
                merged_tombstones[path] = info
    merged["tombstones"] = merged_tombstones

    return merged


def _cleanup_conflict_copies(
    backend: LocalBackend, device_id: str, passphrase: str, memory_kb: int
) -> int:
    """Delete conflict copies for a device's manifest.

    Call ONLY from mutating operations (push, pull) after the manifest
    has been successfully used. Never from status, diff, gc, or dry-run.

    A validator predicate (decrypt + deserialize_manifest) gates deletion:
    candidates whose name matches the iCloud/Dropbox rename patterns but
    which don't deserialize as Mind Meld manifests are left on disk with
    a stderr warning. Only real manifest conflict copies are removed.
    """
    mkey = manifest_key(device_id)
    is_valid = _make_manifest_validator(passphrase, memory_kb)
    return backend.delete_conflict_copies(mkey, is_valid)


def _print_diff_summary(diff: DiffResult, elapsed: float) -> None:
    if not diff.has_changes:
        console.print("[green]Nothing to sync — everything is up to date.[/green]")
        return

    if diff.new:
        console.print(f"  [green]+ {len(diff.new)} new[/green]")
    if diff.modified:
        console.print(f"  [yellow]~ {len(diff.modified)} modified[/yellow]")
    if diff.deleted:
        console.print(f"  [red]- {len(diff.deleted)} deleted[/red]")
    console.print(f"  {len(diff.unchanged)} unchanged")
    console.print(f"  Completed in {elapsed:.1f}s")


def _predict_pull_outcome(
    rel_path: str,
    remote_info: dict,
    base_path: Path,
) -> str:
    """Predict what _apply_incoming_file will do for this file, without applying.

    Returns one of: write, merge, skip, conflict, unchanged. Used by the pull
    dry-run and the diff command to give the user an accurate preview.
    """
    local_path = base_path / rel_path
    if not local_path.exists():
        return "write"
    try:
        local_hash = hash_file(local_path)
    except (PermissionError, OSError):
        return "conflict"  # safest guess — will surface as a real conflict on apply
    if local_hash == remote_info.get("sha256"):
        return "unchanged"
    if should_merge(rel_path):
        # Conservative: predicting "unchanged" for a no-op line-union merge
        # would require downloading + decrypting the blob here (the
        # manifest sha differs because local has lines remote doesn't),
        # which dry-run can't afford. Real pull suppresses no-op merges
        # in `_apply_merge`; dry-run may slightly over-count merges by
        # comparison.
        return "merge"
    try:
        local_mtime = mtime_from_path(local_path)
        remote_mtime_str = remote_info.get("mtime")
        remote_mtime = mtime_from_manifest(remote_mtime_str) if remote_mtime_str else None
    except (ValueError, OSError):
        return "conflict"
    if remote_mtime is not None and local_mtime > remote_mtime:
        return "skip"
    return "conflict"


def _print_pull_prediction(diff: DiffResult, base_path: Path, src_name: str) -> None:
    """Print per-file predicted outcomes for the pull dry-run path.

    Splits diff.modified into skip/merge/conflict buckets so the user can
    see what pull would actually do, not just a "modified" count.
    """
    console.print(f"  [dim]source '{safe_str(src_name)}' ({safe_str(base_path)}):[/dim]")
    for path, info in sorted(diff.new.items()):
        console.print(f"    [green]+ write[/green]    {safe_str(path)}")
    buckets: dict[str, list[str]] = {"merge": [], "skip": [], "conflict": [], "unchanged": []}
    for path, info in diff.modified.items():
        buckets[_predict_pull_outcome(path, info, base_path)].append(path)
    for path in sorted(buckets["merge"]):
        console.print(f"    [cyan]~ merge[/cyan]    {safe_str(path)}")
    for path in sorted(buckets["skip"]):
        console.print(f"    [dim]= skip[/dim]     {safe_str(path)} (local newer)")
    for path in sorted(buckets["conflict"]):
        console.print(
            f"    [yellow]! conflict[/yellow] {safe_str(path)} "
            "(would write remote to .sync-conflict-*)"
        )
    for path in sorted(buckets["unchanged"]):
        console.print(f"    [dim]  unchanged[/dim] {safe_str(path)}")


# ── shared helpers ────────────────────────────────────────────────────


def _upload_changed_blobs(
    backend: LocalBackend,
    base_path: Path,
    to_upload: dict[str, dict],
    device_id: str,
    passphrase: str,
    memory_kb: int,
    verbose: bool = False,
    src_name: str | None = None,
) -> int:
    """Upload changed blobs to storage.

    Reads and hashes each file atomically with read_and_hash to avoid
    TOCTOU races. Returns total encrypted bytes transferred.

    `src_name` (when provided) drives `pullhistory.append` so the history
    log records one "uploaded" entry per file. Optional so legacy/test
    callers without source context still work; production push paths
    always pass it.
    """
    bytes_transferred = 0
    for rel_path, info in to_upload.items():
        file_path = base_path / rel_path
        if not file_path.exists():
            if verbose:
                console.print(f"  [dim]skipped (missing): {safe_str(rel_path)}[/dim]")
            continue

        data, _sha = read_and_hash(file_path)
        enc_data = encrypt(data, passphrase, memory_kb)
        bkey = blob_key(device_id, info["sha256"])
        backend.put(bkey, enc_data)
        bytes_transferred += len(enc_data)

        if src_name is not None:
            pullhistory.append(
                verb="push",
                device=device_id,
                source=src_name,
                rel_path=rel_path,
                action="uploaded",
                local_sha=info.get("sha256"),
            )

        if verbose:
            console.print(f"  [green]\u2191[/green] {safe_str(rel_path)}")

    return bytes_transferred


def conflict_filename(
    canonical: Path,
    device_id: str,
    now: datetime | None = None,
) -> Path:
    """Compute the sibling path used to preserve a remote divergent version.

    Syncthing convention, v1-era (v0.14.0+):
    <stem>.sync-conflict-<YYYYMMDD-HHMMSS>-v1-<device_short>.<ext>

    The ``v1`` token goes AFTER the timestamp, never as a ``v0-``-style
    prefix: a prefix form fails ``is_conflict_filename``, so
    ``_is_excluded`` is False, so the conflict copy would upload to the
    fleet. The timestamp is UTC (``datetime.now(timezone.utc)``) and is
    the sidecar's own birth time; ``st_mtime`` is restored to the peer
    file's clock separately.

    If the computed path already exists (same-second double-conflict on the
    same device), append a 4-char random suffix. Filenames are never overwritten.

    Raises ValueError on empty/None `device_id`. The caller (peer manifest or
    local config) is responsible for supplying a non-empty id; the previous
    `"unknown"` fallback silently minted cross-device-colliding filenames when
    a corrupted peer manifest fed an empty id, which is a data-loss footgun.
    """
    if not device_id:
        raise ValueError("conflict_filename: device_id must be non-empty")
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    device_short = device_id[:8]

    stem = canonical.stem
    suffix = canonical.suffix
    base_name = f"{stem}{CONFLICT_INFIX}{ts}-{CONFLICT_V1_MARKER}-{device_short}"
    path = canonical.with_name(f"{base_name}{suffix}")

    if path.exists():
        rand = secrets.token_hex(2)
        path = canonical.with_name(f"{base_name}-{rand}{suffix}")

    return path


def _prompt_conflict_choice(
    rel_path: str,
    local_path: Path,
    remote_data: bytes,
    peer_name: str | None = None,
    ambiguous_count: int = 0,
    *,
    remote_mtime: datetime | None = None,
) -> tuple[str, bytes | None]:
    """Prompt interactively for how to handle one conflict. Default skip.

    ``peer_name`` is the human-readable name of the device that pushed the
    remote bytes (resolved by the caller via ``lookup_device_by_short_id``);
    ``None`` if unknown or ambiguous. ``ambiguous_count`` is the number of
    matching peers when the device-id prefix collides (>=2); zero otherwise.
    Both flow into the REMOTE banner so the user sees attribution at the
    moment of the choice.

    ``remote_mtime`` is the peer's modified time from the manifest (the
    remote file is not on disk at this site, so this is the only remote
    timestamp available). Keyword-only and trailing so it never shifts the
    positional ``peer_name`` / ``ambiguous_count`` binding at the call site.
    Used for the display + recency verdict only -- there is no ``(n)ewer``
    shortcut here (see the stat block below for why).

    Returns ``(choice, merged_bytes)``. Choice is one of:
    ``keep-canonical`` (= keep local), ``keep-remote``, ``merge``,
    ``abort``, ``keep-both``. ``keep-both`` is what (s)kip emits today --
    the on-disk effect is "leave both files in place." ``merge`` is
    accompanied by ``merged_bytes`` -- the LCS-merged result for the
    caller to write to ``local_path``. For all other choices
    ``merged_bytes`` is ``None``.
    """
    from mind_meld import conflictdiff

    # Stat local for the timestamp display. Best-effort: a failed stat just
    # renders "unknown". The remote side has NO on-disk file at this inline
    # site, so its created/birthtime is genuinely unavailable -- only the
    # manifest's modified time (passed in as ``remote_mtime``) is shown.
    # NOTE: no ``(n)ewer`` shortcut here -- _apply_incoming_file already
    # skipped before prompting when local is newer (cli.py mtime gate), so
    # at this prompt remote is always newer-or-equal and "(n)ewer" would be
    # a redundant alias of (r). Display + verdict only.
    local_mtime_ts, local_btime_ts = _stat_mtime_btime(local_path)
    remote_mtime_ts = remote_mtime.timestamp() if remote_mtime is not None else None

    local_read_failed = False
    try:
        local_bytes = local_path.read_bytes()
    except OSError:
        # Track the failure separately so the (m) option is not offered
        # against an empty-substitute local. Without this, lcs_merge(b"",
        # remote) returns remote as a "clean merge" with default-key (m)
        # and the user would silently overwrite an unreadable-but-extant
        # local with peer bytes (EACCES race, transient FS failure,
        # iCloud placeholder hiccup).
        local_bytes = b""
        local_read_failed = True
    local_text = local_bytes.decode("utf-8", errors="replace").splitlines()
    remote_text = remote_data.decode("utf-8", errors="replace").splitlines()

    # Inline pull-time prompts post-inversion only -- _apply_conflict only
    # produces post-inversion sidecars. local_bytes IS the local side;
    # remote_data IS the peer side; lcs_merge returns conflict_count = -1
    # when either side is binary so we can suppress (m).
    if local_read_failed:
        merged_bytes, merge_conflicts = b"", -1
    else:
        merged_bytes, merge_conflicts = lcs_merge(local_bytes, remote_data)
    # A single-line side gives lcs_merge nothing to align on, so (m) could only
    # ever emit one marker region wrapping both versions whole. Suppress it
    # through the same gate as binary content.
    merge_available = merge_conflicts >= 0 and conflictdiff.merge_has_line_structure(
        local_text, remote_text
    )

    safe_rel = safe_str(rel_path)
    diff = list(
        difflib.unified_diff(
            local_text,
            remote_text,
            fromfile=f"local {safe_rel}",
            tofile=f"remote {safe_rel}",
            lineterm="",
            n=3,
        )
    )

    console.print(f"\n[bold yellow]Conflict:[/bold yellow] {safe_rel}")
    console.print(render_banner("local", local_path.name, None))
    console.print(render_time_line([("modified", local_mtime_ts), ("created", local_btime_ts)]))
    console.print(
        render_banner(
            "remote",
            local_path.name,
            peer_name,
            ambiguous_count=ambiguous_count,
        )
    )
    console.print(render_time_line([("modified", remote_mtime_ts)]))
    _verdict = render_verdict(local_mtime_ts, remote_mtime_ts)
    if _verdict is not None:
        console.print(_verdict)

    # Inline pull-time site is post_inversion only (see comment above):
    # diff is local -> remote, so m = local-only, n = remote-only directly.
    m, n, k = count_divergent_lines(diff)
    if k:
        console.print(
            f"  [dim]{m} unique line{'' if m == 1 else 's'} of yours; "
            f"{n} unique line{'' if n == 1 else 's'} from peer; "
            f"{k} total diff lines.[/dim]"
        )

    # Shared rendering owns terminal-safe peer content, but inline pull keeps
    # its historical 60-entry consent window (mm resolve uses 80).
    for renderable in conflictdiff.render_capped_diff(diff, cap=60):
        console.print(renderable)

    # The conflict file is not on disk yet at this site -- _apply_conflict
    # writes it AFTER this function returns "keep-both" -- so render_prompt's
    # "discard <conflict-name>" copy doesn't quite apply at first glance.
    # Use the bare basename of the local file as both labels: the canonical
    # filename in both positions so the user sees the action's target.
    # Suppress drop-count annotations on empty-diff (binary) -- annotating
    # "drops 0 lines" when we couldn't compare would be a false reassurance.
    prompt_m: int | None = m if diff else None
    prompt_n: int | None = n if diff else None
    console.print(
        render_prompt(
            safe_rel,
            safe_rel,
            "post_inversion",
            merge_available=merge_available,
            merge_conflicts=max(merge_conflicts, 0),
            local_only_lines=prompt_m,
            remote_only_lines=prompt_n,
        )
    )
    # Default key is always (s)kip -- never (m)erge. A clean LCS merge of two
    # genuinely-different documents has zero conflict markers, so defaulting
    # Enter to (m) means one keystroke silently accepts a Frankenstein file.
    # (m)erge stays a fully available choice; the user must type it.
    prompt_default = "s"
    choice = typer.prompt("Choice", default=prompt_default, show_default=False).strip().lower()

    # Shared compatibility policy maps only exact b/both values and emits the
    # existing notice. Inline's c/f fallback remains its existing local policy.
    choice = resolveflow._normalize_legacy_skip_choice_and_warn(choice)

    if choice in ("l", "local", "keep-canonical"):
        return "keep-canonical", None
    if choice in ("r", "remote", "keep-remote"):
        return "keep-remote", None
    if choice in ("m", "merge"):
        if merge_available:
            return "merge", merged_bytes
        # (m) was not offered (binary content) -- treat the literal
        # letter as skip rather than writing potentially-empty bytes.
        return "keep-both", None
    if choice in ("a", "abort"):
        return "abort", None
    # Default-or-skip path: any unrecognized input AND (s)kip itself.
    return "keep-both", None


# \u2500\u2500 _apply_incoming_file decision tree \u2500\u2500\u2500
#
#   Re-read local state at apply time (user may have edited since _pull_core
#   snapshotted). Decide outcome:
#
#   local missing                           -> WRITE     (remote -> canonical)
#   local hash == remote hash               -> UNCHANGED
#   should_merge(rel_path)                  -> MERGED    (jsonl / MEMORY.md)
#   local mtime > remote mtime              -> SKIPPED   (local newer)
#   local mtime <= remote mtime + (m)erge   -> MERGED-VIA-LCS  (user confirmed)
#                                              write merged_bytes -> canonical
#   local mtime <= remote mtime             -> CONFLICTED  (v0.9.2 INVERTED)
#        keep canonical at LOCAL bytes (no rename, no rollback)
#        write REMOTE bytes -> .sync-conflict-<ts>-<device>.<ext>
#
#   Pre-v0.9.2 did the opposite (canonical = remote, sidecar = local). The
#   inversion makes the visible `.sync-conflict-*` file hold the surprising
#   bytes, not the working bytes. Pre-inversion files migrated by Track 5E
#   carry a `v0-` prefix in the metadata position so resolve's dual-mode
#   dispatch can pick the right semantics per file.
#
#   Failures (sidecar write) are isolated per-file: local is untouched at
#   canonical because we never overwrite it. Returns "failed" on error.


# ── deferred inline keep-canonical mtime bump (Track 12A) ─────────────
#
#   _apply_incoming_file (per peer × source × file)
#     keep-canonical → _record_inline_bump (canonical untouched)
#     all other branches → return their outcome
#                          │
#   _download_and_apply: after every _apply_incoming_file call
#     outcome in _CANONICAL_WRITE_OUTCOMES → _invalidate_inline_bump
#       (success-only by construction: write/merge/sidecar outcomes are only
#       returned on successful canonical mutation; "failed" / "skipped" /
#       "unchanged" leave canonical untouched and don't invalidate)
#                          │
#   _pull_core: after the device loop, INSIDE the try block
#     _drain_inline_bumps → _bump_canonical_mtime_post_resolve(path, mtime)
#
# Why deferred, not inline: bumping canonical's mtime mid-pull-walk makes a
# LATER peer's _apply_incoming_file mis-classify on the `local_mtime >
# remote_mtime` gate and silently skip — hiding a peer whose mtime falls
# between original-local and the bump value (the v0.12.6 revert). Deferring
# to end-of-batch means every peer is judged against the SAME original
# baseline; the bump value (max over every keep-canonical peer for that
# path) then beats all of them at once.
#
# Why invalidation lives at the _download_and_apply seam, not in
# _apply_incoming_file's per-branch returns: ONE site keyed on the outcome
# enum covers every canonical-mutating path uniformly — keep-remote, inline
# merge, keep-both (the _apply_conflict sidecar), AND the _apply_write
# branch that fires when canonical vanished mid-walk (user `rm`'d the file
# while the blocking prompt waited). The per-branch approach missed
# _apply_write entirely (would silently bump REMOTE bytes as locally-
# authored) and was not success-only for keep-both (would pop on a sidecar
# write failure even though canonical was still local). See
# docs/invariants/conflicts.md.


def _record_inline_bump(
    pending: dict[Path, float] | None,
    canonical: Path | None,
    peer_mtime: float,
) -> None:
    """Record an inline ``keep-canonical`` decision for the end-of-batch drain.

    No-op when ``pending`` is None (non-interactive pull) or ``canonical`` is
    None (library / direct-call test callers). Keyed on the RESOLVED path so a
    symlinked source root or path-spelling alias can't leave a stale entry that
    a later write keyed under the resolved spelling fails to invalidate.
    ``max`` so that when several peers conflict on the same file in one walk,
    the bump beats every peer walked.
    """
    if pending is None or canonical is None:
        return
    pending[canonical] = max(pending.get(canonical, 0.0), peer_mtime)


def _invalidate_inline_bump(
    pending: dict[Path, float] | None,
    canonical: Path | None,
) -> None:
    """Drop a pending inline bump because a later peer changed/left this file.

    Called once at the ``_download_and_apply`` seam when ``_apply_incoming_file``
    returns an outcome in ``_CANONICAL_WRITE_OUTCOMES`` (write / merge / merge-
    via-lcs / conflicted). Success-only by construction: those outcomes are
    only returned on successful canonical mutation; "failed" leaves canonical
    pure local, so the prior keep-canonical decision still stands and the
    bump is NOT popped. Same property for "skipped" (canonical untouched —
    mtime-skip branch) and "unchanged" (sha match).

    An earlier peer's keep-canonical bump is void once a later peer's decision
    for the same file either overwrites canonical (bumping would broadcast the
    later peer's bytes as locally-authored — the _apply_write file-vanished-
    mid-walk hazard) or leaves it unresolved as a sidecar (bumping would
    silently mtime-resolve a conflict the user explicitly left open).
    """
    if pending is None or canonical is None:
        return
    pending.pop(canonical, None)


def _drain_inline_bumps(pending: dict[Path, float] | None) -> None:
    """Apply every recorded inline ``keep-canonical`` bump at end-of-pull-batch.

    Runs once in ``_pull_core`` AFTER every peer/source has been walked. No-op
    when ``pending`` is None (non-interactive pull) or empty. Placed INSIDE
    ``_pull_core``'s try block: a ``typer.Abort()`` from the inline ``(a)bort``
    choice intentionally skips the drain — abort means "stop, I don't trust
    this pull", so half-made decisions are not broadcast to the fleet.
    """
    if not pending:
        return
    for canonical, peer_mtime in pending.items():
        _bump_canonical_mtime_post_resolve(canonical, peer_mtime)


def _apply_write(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    verbose: bool = False,
    remote_mtime_iso: str | None = None,
) -> ApplyOutcome:
    """[W] local has no copy \u2014 atomic_write remote to canonical."""
    try:
        # Deferred durability: per-file fsync=False; end of pull calls
        # fsutil.fsync_dir once per touched parent.
        fsutil.atomic_write_bytes(local_path, plain_data, fsync=False)
    except (OSError, StorageError) as e:
        console.print(f"  [red]write failed:[/red] {safe_str(rel_path)} \u2014 {safe_str(e)}")
        return "failed"
    _restore_mtime_best_effort(local_path, remote_mtime_iso)
    if verbose:
        console.print(f"  [green]\u2193[/green] {safe_str(rel_path)}")
    return "written"


def _apply_merge(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    verbose: bool = False,
) -> ApplyOutcome:
    """[M] mergeable: jsonl / MEMORY.md are line-union safe.

    No-op suppression: if the line-union merge produces bytes byte-identical
    to what's already on disk (the dominant case when local is a strict
    superset of remote, e.g. peer's MEMORY.md already covered by ours), we
    skip the write entirely and return "unchanged". Skipping the write also
    avoids touching mtime \u2014 which would itself fabricate a phantom
    modification on the next push. The "merged" count then reflects only
    files that actually changed, eliminating the "every pull says
    1+ merged" noise users were seeing.
    """
    try:
        local_bytes = local_path.read_bytes()
        merged = merge_file(rel_path, local_bytes, plain_data)
        if merged == local_bytes:
            if verbose:
                console.print(f"  [dim]= {safe_str(rel_path)} (merge no-op)[/dim]")
            return "unchanged"
        fsutil.atomic_write_bytes(local_path, merged, fsync=False)
    except (OSError, StorageError) as e:
        console.print(f"  [red]merge failed:[/red] {safe_str(rel_path)} \u2014 {safe_str(e)}")
        return "failed"
    if verbose:
        console.print(f"  [cyan]merged[/cyan] {safe_str(rel_path)}")
    return "merged"


def _existing_post_inversion_sidecars_from_peer(canonical: Path, device_short: str) -> list[Path]:
    """List post-inversion ``.sync-conflict-*`` siblings of `canonical` from one peer.

    Used by ``_apply_conflict`` to dedup against prior pulls: every pull
    where peer bytes still don't match local would otherwise create
    another timestamped sidecar (``conflict_filename`` always stamps
    ``datetime.now()``), so users were accumulating N near-identical
    sidecars per peer over N pulls.

    Skips ``v0-``-prefixed pre-inversion sidecars. Those hold LOCAL
    bytes from a pre-v0.9.2 conflict and must NEVER be reaped by the
    apply path -- they encode user data that may not exist anywhere
    else (the local file was renamed out under the inverted semantics).
    The user resolves them through ``mm resolve``'s migration path.

    Returns sidecars whose parsed device_short matches ``device_short``.
    Listing failures (permission, transient FS errors) return empty so
    the caller falls through to the existing write path.
    """
    parent = canonical.parent
    pattern = f"{canonical.stem}{CONFLICT_INFIX}*{canonical.suffix}"
    out: list[Path] = []
    try:
        candidates = list(parent.glob(pattern))
    except OSError:
        return []
    for sibling in candidates:
        if not sibling.is_file():
            continue
        if not is_conflict_filename(sibling.name):
            continue
        if is_pre_inversion_conflict_filename(sibling.name):
            continue
        if parse_conflict_device_short(sibling.name) != device_short:
            continue
        out.append(sibling)
    return out


def _apply_conflict(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    remote_device_id: str,
    verbose: bool = False,
    remote_mtime_iso: str | None = None,
) -> ApplyOutcome:
    """[C] conflict path (v0.9.2 INVERTED): keep local at canonical,
    route remote bytes to .sync-conflict-*.

    Pre-v0.9.2 (Tracks 5A/5B and earlier) did the opposite: rename local
    out to the sidecar, write remote to canonical. Track 5E inverted the
    default for two reasons: (1) asymmetric blast radius \u2014 local is the
    known-working version on this machine, remote is the unknown-from-
    elsewhere version; (2) the visible `.sync-conflict-*` should hold the
    *surprising* version, not the working one. Mtime-skip already
    handles "local newer," so the conflict path only fires when remote
    is newer or mtimes are equal \u2014 but "remote newer" never meant "remote
    correct for this machine."

    Per-peer dedup (post-v0.11.4): before writing, scan for existing
    post-inversion sidecars from this peer for the same canonical. If
    one already holds the same bytes, skip the write -- the prior
    sidecar already represents this conflict, and stamping a fresh
    timestamp would just accumulate near-identical files. If existing
    sidecars hold STALE bytes (peer pushed something newer), reap them
    before writing the new one so the user sees one current sidecar
    per peer rather than a timeline of every pull.

    Failure modes (per-file isolation; never destroys local without a
    recoverable trail):
      * sidecar path-build (empty/None remote_device_id from corrupt peer
        manifest): warn, fail this file, keep walking.
      * sidecar write fail: warn, fail this file. Local is untouched at
        canonical because we never wrote it out \u2014 the inversion makes
        rollback unnecessary.
    """
    # Per-peer dedup. Empty/None remote_device_id falls through to the
    # ValueError branch below where conflict_filename refuses to mint a
    # path -- skip the dedup scan for that case (we couldn't attribute
    # any existing sidecar to this peer anyway).
    if remote_device_id:
        device_short = remote_device_id[:8]
        existing = _existing_post_inversion_sidecars_from_peer(local_path, device_short)
        for sidecar in existing:
            try:
                if sidecar.read_bytes() == plain_data:
                    # Idempotent: peer bytes unchanged from a prior pull;
                    # the existing sidecar already represents this conflict.
                    # Skip the write so we don't accumulate duplicates.
                    if verbose:
                        console.print(
                            f"  [yellow]conflict (unchanged):[/yellow] "
                            f"{safe_str(rel_path)} "
                            f"(existing sidecar {safe_str(sidecar.name)})"
                        )
                    return "conflicted"
            except OSError:
                # Stat/read failure on one candidate -- treat as non-match
                # and let the reap-and-write path handle it.
                continue
        # No content match. Reap stale snapshots from this peer (peer
        # pushed something different since the last sidecar) before
        # writing the new one. Best-effort: unlink failures degrade to
        # accumulation, never block the write.
        for sidecar in existing:
            try:
                sidecar.unlink()
            except OSError as e:
                print(
                    f"mm: warning: stale sidecar unlink failed: "
                    f"{safe_str(sidecar.name)} \u2014 {safe_str(e)}",
                    file=sys.stderr,
                )

    try:
        conflict_path = conflict_filename(local_path, remote_device_id)
    except ValueError as e:
        # Empty/None remote_device_id (corrupted peer manifest). Preserve
        # per-file isolation: warn and fail this file only, keep walking.
        # The pull summary's `failed` count surfaces the issue without
        # losing progress on N other peer files.
        console.print(
            f"  [red]conflict path build failed (local preserved):[/red] "
            f"{safe_str(rel_path)} \u2014 {safe_str(e)}"
        )
        return "failed"

    # Inverted semantics: write remote bytes to the .sync-conflict-* sidecar.
    # Canonical (local) is untouched \u2014 no rename + rollback dance needed
    # because we never overwrite local in the conflict path. The sidecar
    # filename carries a `v1` era marker AFTER the timestamp (v0.14.0+).
    # The `v0-` prefix is reserved for pre-inversion files migrated by
    # `_migrate_pre_inversion_conflict`, NOT new files produced post-inversion.
    try:
        fsutil.atomic_write_bytes(conflict_path, plain_data, fsync=False)
    except (OSError, StorageError) as e:
        console.print(
            f"  [red]sidecar write failed (local preserved):[/red] "
            f"{safe_str(rel_path)} \u2014 {safe_str(e)}"
        )
        return "failed"
    _restore_mtime_best_effort(conflict_path, remote_mtime_iso)

    if verbose:
        console.print(
            f"  [yellow]conflict:[/yellow] {safe_str(rel_path)} "
            f"(remote saved as {safe_str(conflict_path.name)})"
        )
    return "conflicted"


def _apply_incoming_file(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    remote_info: dict,
    remote_device_id: str,
    interactive_resolve: bool = False,
    verbose: bool = False,
    devices: list[dict[str, Any]] | None = None,
    pending_inline_bumps: dict[Path, float] | None = None,
    resolved_local: Path | None = None,
) -> ApplyOutcome:
    """Dispatch one decrypted remote file to the appropriate _apply_* helper.

    See the decision-tree comment above for branch semantics. The local file
    is never destroyed without a recoverable trail (either conflict copy
    or rollback).

    ``pending_inline_bumps`` / ``resolved_local`` carry the Track 12A deferred
    keep-canonical bump. In interactive pull, the keep-canonical branch RECORDS
    into the dict; INVALIDATION is owned by ``_download_and_apply`` keyed on
    the returned outcome (see ``_CANONICAL_WRITE_OUTCOMES``). Both default
    None for non-interactive callers and direct-call tests — when either is
    None the bump machinery is a no-op (today's behavior).
    """
    # Direct callers bypass _download_and_apply's component check, so protect
    # a leaf symlink here before mkdir or atomic_write can replace it.
    if local_path.is_symlink():
        if verbose:
            console.print(
                f"  [yellow]skipped (local symlink preserved):[/yellow] {safe_str(rel_path)}"
            )
        return "skipped"

    local_path.parent.mkdir(parents=True, exist_ok=True)
    remote_mtime_iso = remote_info.get("mtime")

    if not local_path.exists():
        return _apply_write(
            local_path,
            rel_path,
            plain_data,
            verbose=verbose,
            remote_mtime_iso=remote_mtime_iso,
        )

    # Re-read local state. Precomputed snapshot can be stale if the user
    # edited the file after _pull_core built its diff.
    try:
        local_hash = hash_file(local_path)
    except (PermissionError, OSError) as e:
        console.print(f"  [yellow]read failed:[/yellow] {safe_str(rel_path)} \u2014 {safe_str(e)}")
        return "failed"

    if local_hash == remote_info.get("sha256"):
        return "unchanged"

    if should_merge(rel_path):
        return _apply_merge(local_path, rel_path, plain_data, verbose=verbose)

    # [S] local is newer. Keep local at canonical path \u2014 next push propagates it.
    remote_mtime_str = remote_mtime_iso
    local_mtime: datetime | None = None
    remote_mtime: datetime | None = None
    try:
        local_mtime = mtime_from_path(local_path)
        if remote_mtime_str:
            remote_mtime = mtime_from_manifest(remote_mtime_str)
    except (ValueError, OSError) as e:
        # Malformed mtime or filesystem error: fall through to conflict path.
        console.print(
            f"  [yellow]mtime parse failed (forcing conflict):[/yellow] "
            f"{safe_str(rel_path)} \u2014 {safe_str(e)}"
        )
        local_mtime = None
        remote_mtime = None

    if local_mtime is not None and remote_mtime is not None and local_mtime > remote_mtime:
        if verbose:
            console.print(f"  [dim]= {safe_str(rel_path)} (local newer, kept)[/dim]")
        return "skipped"

    # [C] conflict path. Optionally prompt the user; default keep-both.
    if interactive_resolve:
        # Resolve the remote_device_id against the cached devices list
        # so the inline banner shows "(from <peer_name>)" rather than
        # "(unknown peer)" when attribution is available. Falls back
        # cleanly when devices is None (unit tests, library callers).
        peer_name: str | None = None
        ambiguous_count = 0
        if devices:
            short = remote_device_id[:8]
            match, count = lookup_device_by_short_id(devices, short)
            if match is not None:
                peer_name = match.get("device_name")
            elif count > 1:
                ambiguous_count = count
        choice, merged_bytes = _prompt_conflict_choice(
            rel_path,
            local_path,
            plain_data,
            peer_name,
            ambiguous_count,
            remote_mtime=remote_mtime,
        )
        if choice == "keep-canonical":
            # Post-inversion: canonical IS local, so "keep-canonical" =
            # "keep-local" — both work as user-facing labels. The internal
            # outcome stays "skipped" for back-compat with PullResult.
            #
            # Track 12A: record the decision for the end-of-pull-batch drain
            # instead of bumping canonical's mtime here. A mid-walk bump makes
            # a LATER peer's _apply_incoming_file mis-classify on the
            # `local_mtime > remote_mtime` gate and silently skip (the v0.12.6
            # revert). Deferring means every peer is judged against the same
            # original-local baseline; _drain_inline_bumps applies one bump
            # past max(peer_mtime) after the whole pull. peer_mtime is None
            # when the manifest mtime is missing/malformed (conflict path
            # still fires) -- record 0.0, mirroring the resolve-side
            # stat-failure degradation. NOTE: a peer with no parseable mtime
            # never reaches the mtime gate at all, so the bump can't close the
            # resolve->pull loop for THAT peer -- it still helps every
            # mtime-bearing peer.
            peer_ts = remote_mtime.timestamp() if remote_mtime else 0.0
            _record_inline_bump(pending_inline_bumps, resolved_local, peer_ts)
            if verbose:
                console.print(f"  [dim]= {safe_str(rel_path)} (kept local by user)[/dim]")
            return "skipped"
        if choice == "keep-remote":
            # User overrode default keep-both by picking remote \u2014 overwrite
            # canonical with remote bytes. Same I/O as _apply_write but with
            # a different verbose string ("remote kept by user").
            try:
                fsutil.atomic_write_bytes(local_path, plain_data, fsync=False)
            except (OSError, StorageError) as e:
                console.print(
                    f"  [red]write failed:[/red] {safe_str(rel_path)} \u2014 {safe_str(e)}"
                )
                return "failed"
            _restore_mtime_best_effort(local_path, remote_mtime_iso)
            if verbose:
                console.print(
                    f"  [yellow]\u2193[/yellow] {safe_str(rel_path)} (remote kept by user)"
                )
            return "written"
        if choice == "merge":
            # User accepted the LCS-merged result inline. Write merged
            # bytes to canonical; the sidecar was never created (we
            # suppress _apply_conflict by returning "merged-via-lcs"
            # here). Refusal of (m) when merge wasn't offered already
            # mapped to "keep-both" inside _prompt_conflict_choice.
            assert merged_bytes is not None  # invariant: choice=="merge" -> bytes
            try:
                fsutil.atomic_write_bytes(local_path, merged_bytes, fsync=False)
            except (OSError, StorageError) as e:
                console.print(
                    f"  [red]merge write failed:[/red] {safe_str(rel_path)} — {safe_str(e)}"
                )
                return "failed"
            if verbose:
                console.print(f"  [cyan]merged[/cyan] {safe_str(rel_path)} (LCS)")
            return "merged-via-lcs"
        if choice == "abort":
            raise typer.Abort()
        # choice == "keep-both" -> fall through to _apply_conflict, returns
        # "conflicted" on success. _download_and_apply will invalidate any
        # prior keep-canonical bump for this path on that outcome.

    return _apply_conflict(
        local_path,
        rel_path,
        plain_data,
        remote_device_id,
        verbose=verbose,
        remote_mtime_iso=remote_mtime_iso,
    )


def _download_and_apply(
    backend: LocalBackend,
    base_path: Path,
    to_download: dict[str, dict],
    source_device_id: str,
    passphrase: str,
    memory_kb: int,
    interactive_resolve: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    devices: list[dict[str, Any]] | None = None,
    pending_inline_bumps: dict[Path, float] | None = None,
) -> tuple[int, dict[ApplyOutcome, list[str]]]:
    """Download blobs and dispatch each to _apply_incoming_file.

    Returns (encrypted_bytes_transferred, outcomes_by_path).
    outcomes_by_path groups rel_paths by outcome so callers can report
    per-outcome totals and write accurate sync logs.

    ``pending_inline_bumps`` is the shared Track 12A accumulator. This function
    owns the INVALIDATION half of the bump lifecycle: after every
    ``_apply_incoming_file`` call, if the returned outcome is in
    ``_CANONICAL_WRITE_OUTCOMES``, the corresponding entry is popped. RECORD
    stays inside ``_apply_incoming_file``'s keep-canonical branch. Both halves
    no-op when ``pending_inline_bumps`` is None (non-interactive pulls).

    Progress display (Track 5B Task 4):
      - quiet=True: silent. Autopull contract — no stdout/stderr noise.
      - TTY + not quiet: Rich Progress widget renders bar + count + elapsed.
        First-pull-on-new-Mac case (the 2026-04-24 first-pull session): per-file
        backend.get(bkey) blocks on iCloud placeholder materialization;
        before this, the entire 286-file / 263s pull was indistinguishable
        from a hung process. Progress only updates BETWEEN files (a single
        blocking .get() does not yield), so this surfaces stalls at
        per-file boundaries — not intra-file.
      - non-TTY + not quiet: skip the rewriting widget (line-rewriting
        garbles log capture); a one-time start banner keeps scripted
        callers informed without spamming.
      - to_download empty: skip the widget entirely (Rich Progress with
        total=0 risks empty-bar / div-by-zero rendering).
    """
    bytes_transferred = 0
    outcomes: dict[ApplyOutcome, list[str]] = {
        "written": [],
        "merged": [],
        "merged-via-lcs": [],
        "skipped": [],
        "conflicted": [],
        "unchanged": [],
        "failed": [],
    }

    total = len(to_download)
    show_progress = bool(to_download) and not quiet
    use_widget = show_progress and console.is_terminal

    progress: Progress | None = None
    task_id: int | None = None
    if use_widget:
        progress = Progress(
            TextColumn("  [bold]downloading[/bold]"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
        progress.start()
        task_id = progress.add_task("download", total=total)
    elif show_progress:
        # Non-TTY: emit a single start banner. Per-file lines would spam
        # logs; the totals line at the end of pull surfaces the result.
        console.print(f"  downloading {total} file(s)...")

    def _advance() -> None:
        if progress is not None and task_id is not None:
            progress.advance(task_id)

    # Quiet-mode contract: per-file decorations skipped in quiet (autopull);
    # the summary routes per-source totals to stderr in _print_pull_summary
    # (D11). Without this gate, autopull leaked per-file lines to stdout on
    # bad-blob/decrypt errors. (codex /review v0.9.0 caught the gap.)
    try:
        for rel_path, info in to_download.items():
            try:
                bkey = blob_key(source_device_id, info.get("sha256", ""))
            except ValueError as e:
                # Malicious or corrupt manifest shipped a sha256 with path
                # separators / parent-dir refs / null bytes / empty. Per-file
                # isolation: fail this file, keep walking — matches the
                # v0.8.1 empty-device_id handling in _apply_conflict.
                if not quiet:
                    console.print(
                        f"  [red]bad blob key (local preserved):[/red] "
                        f"{safe_str(rel_path)} \u2014 {safe_str(e)}"
                    )
                outcomes["failed"].append(rel_path)
                _advance()
                continue
            try:
                enc_data = backend.get(bkey)
            except MindMeldError:
                if verbose and not quiet:
                    console.print(f"  [yellow]blob missing: {safe_str(bkey)}[/yellow]")
                outcomes["failed"].append(rel_path)
                _advance()
                continue

            try:
                plain_data = decrypt(enc_data, passphrase, memory_kb)
            except CryptoError as e:
                if not quiet:
                    console.print(
                        f"  [red]decrypt failed:[/red] {safe_str(rel_path)} \u2014 {safe_str(e)}"
                    )
                outcomes["failed"].append(rel_path)
                _advance()
                continue

            bytes_transferred += len(enc_data)
            local_path = base_path / rel_path

            # Reject a local link at the destination or in a child component
            # before resolving containment. The root itself may be symlinked,
            # but following a link below it would either escape the source or
            # let atomic_write_bytes replace local routing with peer content.
            if _has_symlinked_component(local_path, base_path):
                if not quiet:
                    console.print(
                        f"  [yellow]skipped (local symlink preserved):[/yellow] "
                        f"{safe_str(rel_path)}"
                    )
                outcomes["skipped"].append(rel_path)
                _advance()
                continue

            # Belt-and-braces: load_manifest already rejects rel_paths
            # that could escape via '..' / absolute / null-byte (see
            # manifest._validate_rel_path). Re-check the FULLY-RESOLVED
            # path here so any future load path that bypasses load_manifest
            # (test fixtures, legacy on-disk caches) still cannot direct
            # writes outside the source root. resolve(strict=False) handles
            # not-yet-created files; matching .resolve() on both sides
            # normalizes symlinks consistently so legit symlinked source
            # roots don't false-positive.
            try:
                resolved_local = local_path.resolve(strict=False)
                resolved_base = base_path.resolve(strict=False)
                _path_inside_base = resolved_local.is_relative_to(resolved_base)
            except (OSError, ValueError):
                _path_inside_base = False
            if not _path_inside_base:
                if not quiet:
                    console.print(
                        f"  [red]rejected (would escape source root):[/red] {safe_str(rel_path)}"
                    )
                outcomes["failed"].append(rel_path)
                _advance()
                continue

            outcome = _apply_incoming_file(
                local_path=local_path,
                rel_path=rel_path,
                plain_data=plain_data,
                remote_info=info,
                remote_device_id=source_device_id,
                interactive_resolve=interactive_resolve,
                verbose=verbose and not quiet,
                devices=devices,
                pending_inline_bumps=pending_inline_bumps,
                resolved_local=resolved_local,
            )
            outcomes[outcome].append(rel_path)
            # Track 12A: centralized eligibility gate for the deferred
            # keep-canonical bump. Any successful canonical mutation
            # (write / merge / merge-via-lcs / conflicted-sidecar) voids a
            # prior peer's pending keep-canonical decision for this resolved
            # path. Success-only by construction — see _CANONICAL_WRITE_OUTCOMES.
            if outcome in _CANONICAL_WRITE_OUTCOMES:
                _invalidate_inline_bump(pending_inline_bumps, resolved_local)
            _advance()
    finally:
        # progress.stop() in its own try/except so a Rich render failure
        # at teardown does not mask the original exception from the loop.
        # (codex /review v0.9.0)
        if progress is not None:
            try:
                progress.stop()
            except Exception:
                pass

    return bytes_transferred, outcomes


# ── init ──────────────────────────────────────────────────────────────


@dataclass
class _StorageOccupancy:
    """Authoritative occupancy signals for the init guard.

    `devices/` entries alone are NOT a trustworthy signal \u2014 list_devices
    silently drops malformed entries, so an attacker or corruption event
    could leave `devices/` empty while `data/` and `manifests/` still
    hold load-bearing encrypted state. The init guard checks the stronger
    signals (mm-crypto-init + blobs + manifests) first.
    """

    has_crypto_init: bool  # fetch_crypto_init().status == "ok"
    has_corrupt_crypto_init: bool
    has_any_blobs: bool  # any data/**/*.enc
    has_any_manifests: bool  # any manifests/**/*.enc
    has_any_devices: bool  # devices/ non-empty (weakest signal)


def _probe_storage_occupancy(backend: LocalBackend) -> _StorageOccupancy:
    """Check which kinds of state a storage root already holds.

    Called by `init` before prompting for a passphrase. Each probe is
    cheap (list_keys with a prefix) and degrades to False on any error,
    so a racy backend never prevents init from continuing.
    """
    fetch = fetch_crypto_init(backend)

    def _any_enc(prefix: str) -> bool:
        try:
            for k in backend.list_keys(prefix):
                if k.endswith(".enc"):
                    return True
        except Exception:
            return False
        return False

    def _has_devices() -> bool:
        try:
            for _ in backend.list_keys(DEVICES_PREFIX):
                return True
        except Exception:
            return False
        return False

    return _StorageOccupancy(
        has_crypto_init=(fetch.status == "ok"),
        has_corrupt_crypto_init=(fetch.status == "corrupt"),
        has_any_blobs=_any_enc(DATA_PREFIX),
        has_any_manifests=_any_enc(MANIFESTS_PREFIX),
        has_any_devices=_has_devices(),
    )


def _init_storage_guard(
    occupancy: _StorageOccupancy,
    existing_device_id: str | None,
    existing_device_name: str | None,
) -> None:
    """Gate init when storage already holds state. Exits nonzero on refuse.

    Two tiers, priority-ordered by severity:

      * BRICK case \u2014 mm-crypto-init is MISSING but blobs or manifests
        exist. About to rebootstrap a new root_salt, which makes every
        existing blob unrecoverable. Refuse by default; require exact
        typed "BRICK" (case-sensitive) to proceed. Non-TTY aborts via
        typer.prompt.

      * ORPHAN case \u2014 mm-crypto-init is ok AND any other occupancy
        signal is true. A new device_id gets minted; old blobs remain
        readable under the shared root_salt but the old device entry
        no longer pushes or pulls. Warn + typer.confirm. Non-TTY aborts.
    """
    # BRICK first: most severe.
    if not occupancy.has_crypto_init and (occupancy.has_any_blobs or occupancy.has_any_manifests):
        stderr_console.print(
            "[red]DANGER:[/red] mm-crypto-init is missing from storage, but "
            "encrypted blobs/manifests still exist. Initializing now generates "
            "a NEW root_salt \u2014 [bold]every existing blob becomes "
            "unrecoverable[/bold]. If another device still has a working "
            "mm-crypto-init in its iCloud cache, wait for sync to reconcile "
            "and retry init instead."
        )
        typed = typer.prompt('Type "BRICK" (case-sensitive) to confirm and proceed')
        if typed != "BRICK":
            stderr_console.print("[yellow]Aborted.[/yellow] No state changed.")
            raise typer.Exit(1)
        return

    # Orphan case: storage has state and we're about to add a device entry.
    any_storage = (
        occupancy.has_any_blobs or occupancy.has_any_manifests or occupancy.has_any_devices
    )
    if occupancy.has_crypto_init and any_storage:
        if existing_device_id:
            msg = (
                f"This creates a new device entry, orphaning existing device "
                f"'{existing_device_id}' ({existing_device_name or 'unknown'}). "
                f"Old blobs remain readable under the shared root_salt but the "
                f"old device entry will no longer push or pull. Proceed?"
            )
        else:
            msg = (
                "Storage already holds encrypted state. A new device_id will "
                "be minted and registered alongside the existing devices. "
                "Proceed?"
            )
        if not typer.confirm(msg, default=False):
            raise typer.Exit()
        return

    # Not gated: first-device path on empty storage, or mm-crypto-init
    # is ok and nothing else exists.


def _prompt_source_toggle(
    source: dict[str, Any], *, current_state: bool, detected: bool | None = None
) -> bool:
    """One Y/n confirm for a single source.

    Single source of truth for the prompt copy + default-Y/N rule. Used
    by `_prompt_sources` (init flow) and `reconfigure_sources` (eng-review
    D5). `current_state` is the default answer:
      - init: whether the source has a qualifying on-disk tree (Grok requires
        one of its hardcoded customization dirs, not merely ~/.grok)
      - reconfigure: whether the source is currently active in config
    """
    name = source["name"]
    path_str = str(source.get("path", ""))
    if path_str:
        if detected is None:
            detected = _source_path_is_detected(source)
        detection_label = "detected" if detected else "not detected"
        prompt = f"Sync '{name}' source at {path_str}? ({detection_label})"
    else:
        prompt = f"Sync '{name}' source?"
    return typer.confirm(prompt, default=current_state)


def _source_path_is_detected(source: dict[str, Any]) -> bool:
    """Return whether a source has the on-disk state that warrants a Y default.

    A Grok home exists even on an install with no user customizations, so its
    root is not an activation or usage-consent signal. Its three hardcoded
    customization trees are. Explicitly configured Grok sources remain valid
    when those trees are absent; this helper controls only prompt defaults and
    labels.
    """
    path_str = str(source.get("path", ""))
    if not path_str:
        return False
    root = Path(path_str).expanduser()
    if source.get("type") == "grok" or source.get("name") == "grok":
        return _config_module.grok_customization_dirs_exist(root)
    return root.exists()


def _prompt_sources() -> list[dict[str, Any]]:
    """Prompt for each known source type; return the enabled entries.

    User-facing sources (claude, gstack) become a Y/n prompt via
    `_prompt_source_toggle`. Default is Y when the qualifying source tree
    exists on disk, N otherwise — Grok requires one of its hardcoded
    customization dirs because its root always contains local session state.
    This nudges users toward only-enabling-what-they-have without making it
    impossible to enable a source whose directory doesn't exist yet (e.g. new
    machine, same project about to be cloned).

    mm-internal sources (mm-events, Group 7+) auto-include without
    prompting — they're mm-owned infrastructure for fleet-wide features
    (retro-fleet) and shouldn't burden the init UX with a question whose
    only legitimate answer is "yes." Per-machine opt-out is via
    `mm disable-source mm-events` post-init (v0.10.0).

    Source paths stay in tilde-form in the returned dicts so they round-
    trip through TOML readably. `get_sources()` expands at use time.
    """
    enabled: list[dict[str, Any]] = []
    for default in DEFAULT_SOURCES:
        if default["name"] in MM_INTERNAL_SOURCE_NAMES:
            src = get_default_source(default["name"])
            if src is not None:
                enabled.append(src)
            continue
        exists = _source_path_is_detected(default)
        if _prompt_source_toggle(default, current_state=exists, detected=exists):
            src = get_default_source(default["name"])
            if src is not None:
                enabled.append(src)
    return enabled


def _load_prior_device_metadata() -> tuple[str | None, str | None]:
    """Return (id, name) from any existing config, best-effort.

    Used to name the device that would be orphaned by re-init. Malformed
    or missing config returns (None, None) — the orphan warning just
    loses the descriptive name.
    """
    if not _config_module.CONFIG_PATH.exists():
        return None, None
    try:
        prior = load_config()
    except MindMeldError:
        return None, None
    return prior.get("device", {}).get("id"), prior.get("device", {}).get("name")


def _prompt_passphrase(is_first_device: bool) -> str:
    """Prompt for the encryption passphrase. Double-prompt on first-device.

    Exits via _error on empty input or (first-device) mismatch. Returns
    the validated passphrase string — caller uses it for bootstrap or
    verify but MUST NOT commit to keyring until crypto validation passes.
    """
    passphrase = typer.prompt("Encryption passphrase", hide_input=True)
    if not passphrase:
        _error("Passphrase cannot be empty.")
    if is_first_device:
        confirm = typer.prompt("Confirm passphrase", hide_input=True)
        if passphrase != confirm:
            _error("Passphrases don't match.")
    return passphrase


def _bootstrap_or_verify_crypto(
    backend: LocalBackend,
    passphrase: str,
    is_first_device: bool,
    fetch: CryptoInitFetch,
) -> tuple[bytes, int, bytes]:
    """Return (root_salt, argon2_memory_kb, keycheck_blob).

    First-device path: try to bootstrap. On StorageError (lost race), fall
    through to verify against the winner's mm-crypto-init — this is the
    same flow a second device would take.

    Second-device path: verify the passphrase against the existing
    mm-crypto-init. Failure aborts via _error; no local state is written.

    Sets the crypto session as a side effect so downstream calls can
    derive blob-level keys.
    """
    if is_first_device:
        try:
            bootstrap = bootstrap_crypto_init(
                backend, passphrase, argon2_memory_kb=DEFAULT_ARGON2_MEMORY_KB
            )
        except StorageError:
            # Race: another device wrote mm-crypto-init between our fetch and
            # our put. Fall through to second-device verify with the winner's blob.
            console.print(
                "  [dim]Another device bootstrapped concurrently — "
                "verifying against their mm-crypto-init.[/dim]"
            )
            retry_fetch = fetch_crypto_init(backend)
            if retry_fetch.status != "ok":
                _error("init: lost bootstrap race but peer's mm-crypto-init not ok.")
            assert retry_fetch.root_salt is not None
            assert retry_fetch.argon2_memory_kb is not None
            assert retry_fetch.keycheck_blob is not None
            return _verify_existing_crypto_init(
                retry_fetch,
                passphrase,
                success_message="  Verified passphrase against peer mm-crypto-init.",
            )

        assert bootstrap.root_salt is not None
        assert bootstrap.argon2_memory_kb is not None
        assert bootstrap.keycheck_blob is not None
        root_salt = bootstrap.root_salt
        argon2_memory_kb = bootstrap.argon2_memory_kb
        keycheck_blob = bootstrap.keycheck_blob
        set_crypto_session(root_salt, argon2_memory_kb)
        console.print(
            f"  mm-crypto-init bootstrapped (root_salt fp={root_salt_fingerprint(root_salt)})."
        )
        return root_salt, argon2_memory_kb, keycheck_blob

    # Second-device: verify against the fetch we already did.
    return _verify_existing_crypto_init(
        fetch,
        passphrase,
        success_message=(
            "  Verified passphrase against existing mm-crypto-init "
            f"(root_salt fp={root_salt_fingerprint(fetch.root_salt)})."
        ),
    )


def _verify_existing_crypto_init(
    fetch: CryptoInitFetch,
    passphrase: str,
    *,
    success_message: str,
) -> tuple[bytes, int, bytes]:
    """Verify an already-fetched crypto init and activate its session.

    The caller owns any race retry before invoking this helper. In particular,
    `_bootstrap_or_verify_crypto` must never feed its stale pre-bootstrap fetch
    here after an exclusive-create loss.
    """
    assert fetch.root_salt is not None
    assert fetch.argon2_memory_kb is not None
    assert fetch.keycheck_blob is not None
    root_salt = fetch.root_salt
    argon2_memory_kb = fetch.argon2_memory_kb
    keycheck_blob = fetch.keycheck_blob
    set_crypto_session(root_salt, argon2_memory_kb)
    master_key = load_master_key(passphrase, root_salt, argon2_memory_kb)
    try:
        verify_passphrase(master_key, keycheck_blob)
    except CryptoError as e:
        _error(str(e))
    console.print(success_message)
    return root_salt, argon2_memory_kb, keycheck_blob


def _register_and_save(
    config: dict,
    backend: LocalBackend,
    device_id: str,
    device_name: str,
    passphrase: str,
) -> None:
    """Register the device, persist config, and store the passphrase.

    Order matters: device register (storage write) → config write (local
    pointer) → keyring store. The "remote first, local pointer last"
    pattern is the canonical filesystem/DB transaction discipline — a
    crash before the local pointer is committed leaves the remote with
    an inert breadcrumb (recoverable on retry), never the inverse half-
    state where local claims a device storage doesn't recognize. A typo'd
    passphrase on the second-device path is caught in crypto validation
    BEFORE this function runs, so keyring never holds an invalid secret.

    Crash safety (Track 5D, v0.9.4):
      * register fails → re-raise; nothing local was written, retry init
        runs the first-device path naturally.
      * save_config fails → best-effort `backend.delete(devices/<id>.json)`
        before re-raising. Normal save failures (disk full, permissions)
        self-clean so retry init doesn't trip `_init_storage_guard`'s
        orphan-case warning. If the cleanup itself fails, the original
        save error propagates — masking the real cause behind a confusing
        secondary error is worse than the cosmetic orphan that remains.
      * SIGKILL between register and save_config → cosmetic orphan device
        entry in storage. `_init_storage_guard` warns + prompts on retry
        init; user confirms and a fresh device_id is minted alongside.
        The orphan is inert (no `last_seen`, never used).
      * Pre-existing victims of the v0.8.15..v0.9.3 inverse half-state
        (config has device_id, storage lacks devices/<id>.json) self-heal
        on first push via `_ensure_device_registered` at `_push_core`
        entry — see Track 5D Task 2b.
    """
    # Precompute the storage key so the cleanup-warning f-string below
    # cannot itself raise from `device_key(...)` validation and mask the
    # original save_config error (codex adversarial 2026-04-25).
    dev_key = device_key(device_id)
    register_device(backend, device_id, device_name)
    try:
        save_config(config)
    except Exception:
        # Best-effort cleanup: keep the device entry from accumulating in
        # storage when save_config raises normally (disk full, permissions,
        # bad config path). Without this, repeated init failures mint a new
        # orphan storage entry per attempt and trip `_init_storage_guard`'s
        # orphan-case warning on retry. SIGKILL still leaves the cosmetic
        # orphan — that's the rare event we accept structurally via the
        # order swap.
        try:
            backend.delete(dev_key)
        except Exception as cleanup_exc:
            # Per CLAUDE.md visible-failure contract: load-bearing warnings
            # signal data-at-risk degradation and must reach stderr even in
            # quiet mode. Original save error wins via bare `raise` below —
            # surfacing the cleanup failure but not letting it mask the
            # real cause.
            stderr_console.print(
                f"mm: warning: cleanup of {dev_key} after "
                f"save_config failure failed ({type(cleanup_exc).__name__}): "
                f"{cleanup_exc}"
            )
        raise
    console.print(f"  Device registered: {device_name} ({device_id})")
    console.print(f"  Config written to {_config_module.CONFIG_PATH}")

    # store_passphrase_in_keyring narrowed its own catch to
    # (KeyringError, ImportError) in v0.8.9. Init has already committed
    # config + device registration at this point, so aborting on a
    # non-KeyringError kind (e.g., RuntimeError from a broken backend)
    # would leave the user half-initialized with a cryptic traceback.
    # Treat any keyring failure as "not available" + warn; env-var is a
    # valid fallback and the user can re-run init if they want to retry.
    try:
        stored = store_passphrase_in_keyring(passphrase)
    except Exception as e:
        console.print(f"  [yellow]Keyring backend error ({type(e).__name__}):[/yellow] {e}")
        stored = False

    if stored:
        console.print("  Passphrase stored in OS keyring.")
    else:
        console.print(
            "  [yellow]No keyring available.[/yellow] "
            "Set MINDMELD_PASSPHRASE environment variable instead."
        )


_ICLOUD_DRIVE_ROOT = Path("~/Library/Mobile Documents/com~apple~CloudDocs").expanduser()


def _auto_pin_storage_for_icloud(storage_path: Path) -> None:
    """Auto-pin an iCloud Drive storage path so future pulls don't block on
    iCloud File Provider materialization.

    Runs ``brctl download <storage_path>`` (Apple's iCloud File Provider CLI,
    /usr/bin/brctl). brctl is non-destructive, idempotent, and async — it
    queues the request and returns immediately while iCloud materializes
    files in the background. On any error (brctl missing, timeout, non-zero
    exit) falls back to a one-line Finder tip.

    Silent for non-iCloud storage paths: the slow-pull case only exists when
    blobs go cold via the iCloud File Provider; a regular local folder
    doesn't need pinning.

    Called once at init success. If iCloud later evicts blobs (storage
    pressure), the user can re-pin manually via the same command. We do NOT
    re-run on every push/pull — that would be invasive overhead for an
    onboarding nudge.
    """
    try:
        is_icloud = storage_path.resolve(strict=False).is_relative_to(_ICLOUD_DRIVE_ROOT)
    except (OSError, ValueError):
        is_icloud = False

    if not is_icloud:
        return

    try:
        result = subprocess.run(
            ["brctl", "download", str(storage_path)],
            check=False,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            console.print(
                "  [dim]Storage pinned for fast pulls "
                "(iCloud will keep blobs resident on this Mac).[/dim]"
            )
            return
    except (
        FileNotFoundError,
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
        subprocess.SubprocessError,
    ):
        pass

    console.print(
        "  [dim]Tip: keep blobs resident for fast pulls. In Finder, "
        f'right-click "{storage_path}" › Keep Downloaded.[/dim]'
    )


@app.command()
def init() -> None:
    """Initialize Mind Meld: generate device ID, configure iCloud storage, set passphrase.

    Two-path flow. Storage is probed (mm-crypto-init) BEFORE any local state is
    written or any passphrase is committed to the keyring.

    First-device path (mm-crypto-init missing):
        Double-prompt passphrase, generate root_salt + keycheck, atomic write,
        then register device + save config + store passphrase (Track 5D order).

    Second-device path (mm-crypto-init ok):
        Single-prompt passphrase, derive master_key from the stored root_salt,
        verify keycheck. On success, register device + save config + store
        passphrase. On failure, nothing local is written.

    Two-tier re-init guard (Group 2 pre-flight 3):
        * Orphan case — mm-crypto-init ok + other occupancy: warn that
          a new device entry gets created alongside existing devices;
          require typer.confirm.
        * BRICK case — mm-crypto-init missing + blobs/manifests exist:
          re-bootstrap would generate a new root_salt and brick every
          existing blob. Refuse by default; require exact typed "BRICK".
    """
    # Capture prior device metadata BEFORE any prompt so the orphan-case
    # warning can name the device about to be left behind.
    existing_device_id, existing_device_name = _load_prior_device_metadata()
    if _config_module.CONFIG_PATH.exists():
        overwrite = typer.confirm(
            f"Config already exists at {_config_module.CONFIG_PATH}. Overwrite?"
        )
        if not overwrite:
            raise typer.Exit()

    console.print(f"[bold]Mind Meld v{__version__} — init[/bold]\n")

    # Storage path first: we need a backend to probe mm-crypto-init.
    storage_path = typer.prompt("Storage folder path", default=DEFAULT_STORAGE_PATH)
    full_path = Path(storage_path).expanduser()
    full_path.mkdir(parents=True, exist_ok=True)
    console.print(f"  Storage: {full_path}")

    # Lightweight backend (config is not yet written; instantiate directly).
    backend = LocalBackend(str(full_path))

    # Probe storage for mm-crypto-init BEFORE committing any local state.
    fetch = fetch_crypto_init(backend)
    if fetch.status == "corrupt":
        _error(
            "init: mm-crypto-init at storage root is corrupt. If another device "
            "still has a valid copy in its local iCloud cache, wait for sync to "
            "reconcile and retry. Otherwise remove mm-crypto-init manually and "
            "retry init (WARNING: this destroys all existing v2 blobs)."
        )

    # Two-tier guard: storage occupancy is authoritative state, not just
    # local config. Runs BEFORE any further prompt, so a refused init never
    # touches the keyring or writes config.
    occupancy = _probe_storage_occupancy(backend)
    _init_storage_guard(
        occupancy,
        existing_device_id=existing_device_id,
        existing_device_name=existing_device_name,
    )

    is_first_device = fetch.status == "missing"

    # Init-time collision prevention: scan existing peers and regenerate
    # the 8-char device_id on collision. UUID4 prefix collisions are
    # extremely unlikely (~1 in 4 billion per draw) but a deterministic-RNG
    # bug or a cloned-from-snapshot peer could collide reproducibly.
    # Cheap to defend in depth; runtime lookup_device_by_short_id still
    # handles a stray collision if we exhaust the retry budget.
    existing_devices = list_devices(backend) if not is_first_device else []
    device_id = generate_unique_short_device_id(existing_devices)
    device_name = typer.prompt("Device name", default=_default_device_name())

    passphrase = _prompt_passphrase(is_first_device)
    root_salt, argon2_memory_kb, _keycheck_blob = _bootstrap_or_verify_crypto(
        backend, passphrase, is_first_device, fetch
    )

    # All crypto passed; it's safe to prompt for sources and write state.
    # Refuse if no sources enabled — a config with zero sources leaves
    # push/pull silently no-op'ing.
    #
    # Ordering note: source prompt runs AFTER crypto bootstrap to keep the
    # second-device wrong-passphrase path fast — we want "wrong pw" to
    # surface BEFORE we ask about sources, not after. Consequence: on a
    # FIRST-device refuse-all, mm-crypto-init is already written to storage
    # and orphaned. This is benign: re-running init hits the second-device
    # path (mm-crypto-init now exists), user's same passphrase verifies
    # against their own earlier bootstrap, and the second attempt finishes
    # cleanly. No data loss, just a storage-side breadcrumb. See
    # TestInitFlow::test_first_device_refuse_all_is_recoverable.
    sources = _prompt_sources()
    # mm-internal sources (mm-events) auto-include and don't count toward
    # the user-intent guard — a config with only mm-internal sources is
    # effectively "user wanted nothing synced," same as the pre-Group-7
    # zero-sources case. Push/pull would silently no-op for the user's
    # own data; better to refuse and let them re-run.
    user_facing_sources = [s for s in sources if s["name"] not in MM_INTERNAL_SOURCE_NAMES]
    if not user_facing_sources:
        _error("init: no sync sources enabled. Re-run 'mm init' and accept at least one source.")

    config: dict = {
        "device": {"id": device_id, "name": device_name},
        "storage": {"path": storage_path},
        "sync": {
            "sources": sources,
            "max_file_size": DEFAULT_MAX_FILE_SIZE,
        },
        "crypto": {
            "argon2_memory_kb": argon2_memory_kb,
            "root_salt_fp": root_salt_fingerprint(root_salt),
        },
    }

    _register_and_save(config, backend, device_id, device_name, passphrase)

    # Seam 1 — transition detection (init path). Seeds the
    # last_seen_self_version cache so any subsequent upgrade transition is
    # logged correctly. Routed here directly rather than through
    # _get_config / _auto_command_setup because init has its own first-run
    # path with no prior config to load (D6 reasoning).
    upgrade.run_transition_hook(config)

    # Group 8 / Track 8A: drop the retro-fleet skill symlink at init time
    # (no 24h gate here — first-install pass should always try). Idempotent
    # if the user already has a correct symlink. Conflicts emit a one-line
    # notice; failures are forensic-only.
    # Track 25C: resolve sources BEFORE the installer so consent is known.
    # Hook position relative to _register_and_save and _run_events_backfill
    # is unchanged. The mm-events bootstrap mkdir moves a few lines earlier.
    resolved_sources = get_sources(config)
    may_create = skill_link.consented_agent_keys(config, resolved_sources)
    try:
        skill_link._ensure_retro_skill_links(dry_run=False, explicit=True, may_create=may_create)
    except Exception as e:
        stderr_console.print(
            f"mm: notice: retro-fleet skill installation failed: {type(e).__name__}: {safe_str(e)}"
        )

    # Init-time event backfill (v0.11.8). Captures the past 30 days of git
    # commits + a full sessions inventory so retro-fleet works immediately
    # after init, without waiting for the first push to populate events.
    # Resolves sources via get_sources() so mm-events bootstraps the events
    # dir before walk runs. Forensic-only on failure; init proceeds.
    events_tail._run_events_backfill(config, resolved_sources, device_id)

    console.print("\n[green]Mind Meld initialized. Run 'mm push' to sync.[/green]")

    # Track 9A: auto-pin iCloud storage so the user's first pull (and every
    # subsequent pull) reads resident blobs instead of blocking on iCloud
    # File Provider materialization. Best-effort; falls back to a Finder
    # right-click tip on any error. Silent for non-iCloud storage paths.
    _auto_pin_storage_for_icloud(full_path)


# ── push ──────────────────────────────────────────────────────────────


@app.command()
def push(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed output"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change"),
) -> None:
    """Push local session data to storage."""
    config = _get_config()
    _maybe_prompt_migration(config)
    # Re-load in case the migration prompt mutated config on disk so the
    # current command sees the new exclude_patterns.
    config = _get_config()
    passphrase = _get_passphrase_or_exit()

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    try:
        backend = get_backend(config)
        try:
            memory_kb = _init_crypto_session(backend, passphrase, config)
        except MindMeldError as e:
            _error(str(e))
        result = _push_core(config, passphrase, memory_kb, verbose, dry_run)

        # Auto GC on interactive push only (not autopush).
        # Catch only unexpected failures — let typer.Exit (from _do_gc's
        # refuse-on-corrupt path) propagate so the user sees the actionable
        # message. Silent-swallow would hide the safety refusal.
        if result and (result.total_new or result.total_modified or result.total_deleted):
            try:
                gc_count = _do_gc(
                    config,
                    passphrase,
                    memory_kb,
                    dry_run=False,
                    verbose=False,
                    emit_retention_summary=False,
                )
                if gc_count:
                    console.print(f"  GC: deleted {gc_count} orphaned blobs.")
            except typer.Exit:
                raise
            except (OSError, MindMeldError) as e:
                console.print(
                    f"  [yellow]Warning:[/yellow] GC skipped ({e}). "
                    f"Run 'mm gc' manually for details."
                )
    finally:
        release_lock()

    # Seam 2 — interactive push tail nudge.
    upgrade.emit_nudge_if_due(config)


def _ensure_device_registered(
    backend: LocalBackend,
    device_id: str,
    device_name: str,
    *,
    dry_run: bool = False,
) -> None:
    """Self-heal a missing device entry before push runs (Track 5D Task 2b).

    If the device's storage entry is absent, recreate it. Two scenarios
    converge here:
      * SIGKILL/OOM crash between `register_device` and `save_config` in
        a v0.9.4+ init — the order swap (Task 2) accepts this as a rare
        cosmetic event for the *next* init, but a user who got past init
        with a half-state lands here.
      * Pre-v0.9.4 init crash in the v0.8.15..v0.9.3 inverted window:
        local config has a `device_id` that storage's `devices/` doesn't
        contain. Without this hook those users push manifests under an
        ID no peer recognizes, silently. Self-heal makes the fix retro-
        active: first push after upgrade re-registers the device.

    `dry_run=True` is a no-op (codex review 2026-04-25): self-heal does
    a backend.put via register_device, which would violate `mm push
    --dry-run`'s "preview only" contract. The real `mm push` afterwards
    does the heal.

    register_device is idempotent on the storage write and the entry
    carries no `last_seen` until the first successful push completes,
    so a brief race against a concurrent init is benign.

    On register failure (transient iCloud StorageError), emit a stderr
    breadcrumb before re-raising. The breadcrumb is load-bearing for
    autopush, whose generic `except Exception` would otherwise swallow
    the failure and silently no-op every push — `mm: warning:` matches
    the visible-failure contract for degraded-state signals.
    """
    if dry_run:
        return
    if backend.exists(device_key(device_id)):
        return
    try:
        register_device(backend, device_id, device_name)
    except Exception as e:
        stderr_console.print(
            f"mm: warning: device entry self-heal failed ({type(e).__name__}): {e}"
        )
        raise


def _has_mtime_only_changes_vs_remote(
    local_manifest: dict[str, Any],
    remote_sources: dict[str, Any],
    source_filter: str | None = None,
) -> bool:
    """True iff any file in `local_manifest` has identical sha256 but a
    STRICTLY-NEWER mtime versus the same file in `remote_sources`.

    Drives the manifest-republish leg of the resolve(local) fleet-propagation
    fix (v0.12.6). The v0.12.2 substantive-change gate skips `mm push` when
    no source has any `diff_files` change, and `diff_files` keys equality on
    sha256 alone. That gate is correct for "no bytes changed AND no metadata
    needs broadcasting" but wrong when the user just ran `mm resolve` and
    picked (l)ocal -- ``_bump_canonical_mtime_post_resolve`` bumped the mtime
    in-place, sha256 stayed the same, and the gate would silently swallow
    the new mtime. Result: kb-ms breaks its local conflict loop but kb-mbp
    never sees kb-ms's authoritative claim and the fleet stays divergent.

    **Forward-only invariant (load-bearing, Codex P2 catch).** Trips only on
    `local_mtime > remote_mtime`, NOT on `!=`. A push that downgrades the
    manifest's recorded mtime is a silent-skip hazard: a peer holding
    different bytes with a mtime between the old-remote and the downgraded
    value would now hit `local_mtime > remote_mtime` and SKIP the pull
    (where pre-downgrade it would have hit the conflict path). Downgrades
    happen on benign operations like `git checkout` / file-restore /
    `touch -t`, so the forward-only gate is required for correctness, not
    just safety.

    **Parse before compare (Codex P2 5th-pass catch).** ``load_manifest``
    does NOT type-check ``files[*].mtime`` -- a peer with a wrong-typed
    value (e.g. ``mtime: 1234``) or a non-canonical ISO spelling (``Z`` vs
    ``+00:00``) would either crash on a raw ``>`` string compare or
    lexically misorder otherwise-equal timestamps. Both sides parse through
    ``mtime_from_manifest(...).timestamp()`` (same path
    ``_restore_mtime_best_effort`` uses) and any parse failure on either
    side returns "no drift on this file" -- conservative: better to under-
    publish a metadata refresh than crash the gate.

    Files present only in local (`remote_info is None`) are skipped here --
    they're caught as `new` by the existing sha256 gate. Files with mismatched
    sha256 are also skipped -- caught as `modified`. We only fire on the
    intersection: same path, same content, strictly-newer local mtime.

    ``source_filter`` (optional) scopes the walk to a single named source,
    mirroring ``iter_source_diffs``'s same-named arg so callers like
    ``mm status --source <name>`` don't surface metadata-pending hints from
    other sources.
    """
    for src_name, src_data in local_manifest.get("sources", {}).items():
        if source_filter is not None and src_name != source_filter:
            continue
        local_files = src_data.get("files", {})
        remote_src = remote_sources.get(src_name, {})
        remote_files = remote_src.get("files", {})
        for rel_path, local_info in local_files.items():
            remote_info = remote_files.get(rel_path)
            if remote_info is None:
                continue
            if local_info.get("sha256") != remote_info.get("sha256"):
                continue
            local_mt = local_info.get("mtime")
            remote_mt = remote_info.get("mtime")
            if not local_mt or not remote_mt:
                continue
            try:
                local_ts = mtime_from_manifest(local_mt).timestamp()
                remote_ts = mtime_from_manifest(remote_mt).timestamp()
            except (TypeError, ValueError, OverflowError, OSError):
                # Malformed mtime on either side (peer wrote `mtime: 1234`
                # int, or unparseable string). Conservative: treat as
                # non-drifting so we don't republish a manifest with garbage
                # the next push would just re-pull-and-skip.
                continue
            if local_ts > remote_ts:
                return True
    return False


def _push_core(
    config: dict,
    passphrase: str,
    memory_kb: int,
    verbose: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
) -> PushResult | None:
    """Core push logic shared by push and autopush.

    When quiet=True, suppresses all rich console output (for autopush).
    Returns PushResult on success, None if nothing to push or dry_run.
    """
    start = time.time()
    device_id = config["device"]["id"]
    device_name = config["device"]["name"]
    max_file_size = config["sync"]["max_file_size"]

    backend = get_backend(config)
    _ensure_device_registered(backend, device_id, device_name, dry_run=dry_run)

    # Build local manifest (v2 with sources). Hoisted above the skill hook
    # so consent is known before the gate runs (Track 25C). The hook itself
    # stays AFTER _ensure_device_registered and BEFORE _run_events_tail.
    # The mm-events bootstrap mkdir moves a few lines earlier.
    sources = get_sources(config)
    may_create = skill_link.consented_agent_keys(config, sources)

    # Group 8 / Track 8A retro-fleet skill self-heal. Position locked in
    # /plan-eng-review Architecture #5: AFTER device self-heal (storage-
    # write before any walk), BEFORE events tail (local-FS self-heals
    # stacked, events tail is the load-bearing always-runs block). Gated by
    # 24h-TTL — the marker stat is the entire hot-path cost on the steady-
    # state push (~1 syscall). dry_run gates the install too (preview
    # contract; mirrors _ensure_device_registered).
    # The gate and the installer MUST receive the same may_create: a
    # declined row never gets its success marker touched, so an unfiltered
    # gate stays open and runs the full installer prologue on every push.
    if not dry_run:
        try:
            if not quiet:
                # OpenCode retirement is independent of the 24h drift gate.
                # Interactive push is the advertised trigger; a fresh
                # Claude/Codex marker must not skip the reaper. Autopush
                # still does not mutate. Init / install-skills reach the
                # same function via _ensure_retro_skill_links.
                skill_link._reap_retired_opencode_skill_link()
            if skill_link._skill_links_check_due(may_create=may_create):
                skill_link._ensure_retro_skill_links(
                    dry_run=False,
                    allow_mutate=not quiet,
                    may_create=may_create,
                )
        except Exception as e:
            stderr_console.print(
                f"mm: notice: retro-fleet skill installation failed: "
                f"{type(e).__name__}: {safe_str(e)}"
            )
    if not sources:
        msg = "no sync sources found. Run 'mm init' to configure."
        if quiet:
            # Load-bearing: a misconfigured sources list silently no-ops every
            # autopush forever. Surface to stderr so the user notices the
            # broken state (this is distinct from "no config at all", which
            # the caller filters before reaching here).
            print(f"mm: warning: {msg}", file=sys.stderr)
        else:
            console.print(f"[yellow]Warning:[/yellow] {msg}")
        return None

    skipped: list[tuple[str, str]] = []

    def on_skip(path: str, reason: str) -> None:
        skipped.append((path, reason))
        if verbose and not quiet:
            console.print(f"  [dim]skipped: {safe_str(path)} ({safe_str(reason)})[/dim]")

    # Build local manifest BEFORE the events tail. The substantive-change
    # gate below decides whether to write a new mm-push event row at all \u2014
    # without this, every empty `mm push` would write an event, mutate the
    # mm-events file, and report "1 file uploaded" forever (the phantom-
    # change-on-empty-push regression). The events tail's pre-v0.12.2
    # trust boundary "MUST run on every push attempt" is relaxed to "MUST
    # run on every push that uploads bytes" \u2014 the cursor stays accurate
    # because no-op pushes don't advance it (see events.py).
    if not quiet:
        console.print("[bold]Building manifest...[/bold]")
    local_manifest = build_manifest_v2(device_id, device_name, sources, max_file_size, on_skip)

    total_file_count = sum(
        len(src_data["files"]) for src_data in local_manifest["sources"].values()
    )
    if not quiet:
        console.print(
            f"  {total_file_count} files scanned across {len(local_manifest['sources'])} source(s)"
        )
        if skipped:
            console.print(f"  [yellow]{len(skipped)} files skipped[/yellow]")

    # Fetch remote manifest (tri-state: ok / missing / corrupt).
    # `fetch.manifest` (when ok) is pre-normalized via load_manifest;
    # _recover_prior_manifest's sidecar/peer paths emit the same shape.
    fetch = _fetch_remote_manifest(backend, device_id, passphrase, memory_kb)
    remote_manifest = _recover_prior_manifest(
        fetch, backend, device_id, passphrase, memory_kb, quiet=quiet
    )

    # Consumer-boundary filters. Strip from prior_manifest BOTH (1) paths
    # the local config now excludes via per-source `exclude_patterns` and
    # (2) entire sources the user has marked in [sync].disabled_sources.
    # Both filters apply to the ok-fetch path AND the recovery branches
    # (sidecar prior state, peer-tombstone aggregation).
    #
    # Without (1): generate_tombstones emits a deletion tombstone for
    # every newly-excluded path on first post-migration push (the
    # 2026-04-24 first-pull regression).
    #
    # Without (2): generate_tombstones emits a deletion tombstone for
    # every file in a newly-disabled source on first post-disable push,
    # propagating fleet-wide data loss. v0.10.0 source-toggle invariant;
    # mirror of (1)'s pattern. See docs/designs/source-toggle.md.
    #
    # Order matters: disable first, then exclude. Disabling drops the
    # whole source; excluding-then-filtering would walk a soon-to-be-
    # dropped source's exclude_patterns for nothing.
    exclude_map, skip_prefixes = _build_exclude_map(config)
    disabled_sources = list(config.get("sync", {}).get("disabled_sources", []) or [])
    if remote_manifest is not None:
        remote_manifest = _filter_disabled_sources(remote_manifest, disabled_sources)
        remote_manifest = _filter_excluded_paths(remote_manifest, exclude_map, skip_prefixes)
        remote_manifest = _filter_symlinked_paths(remote_manifest, sources)

    # Generate tombstones for files that disappeared since last push
    tombstones = generate_tombstones(local_manifest, remote_manifest, device_id)
    local_manifest["tombstones"] = tombstones

    # Only the REAL remote manifest drives the diff (avoid re-uploading every
    # file just because we're recovering from corruption via the sidecar).
    real_remote = fetch.manifest if fetch.is_ok else None
    remote_sources = real_remote.get("sources", {}) if real_remote else {}

    # Substantive-change gate (v0.12.2). Run BEFORE the events tail so
    # truly empty pushes don't write an mm-push event row that becomes
    # the only "change" pushed. Counts ANY source diff (user OR mm-events
    # \u2014 the latter catches an un-flushed prior push that wrote an event
    # but failed mid-upload). Pre-v0.12.2: events tail fired at HEAD of
    # _push_core unconditionally, so every `mm push` reported at least
    # the events-file modification.
    recovering_from_corrupt = fetch.status == "corrupt"
    has_substantive = any(
        True
        for _, _, _, _ in iter_source_diffs(local_manifest, remote_sources, skip_unchanged=True)
    )
    # mtime-only republish leg (v0.12.6): sha256-equal files with drifted
    # mtime must still trigger a manifest upload so peers see authoritative-
    # local claims from `mm resolve` (l)ocal. See `_has_mtime_only_changes_vs_remote`.
    has_mtime_only = (not has_substantive) and _has_mtime_only_changes_vs_remote(
        local_manifest, remote_sources
    )
    if not has_substantive and not has_mtime_only and not recovering_from_corrupt:
        if not quiet:
            console.print("[green]Nothing to push \u2014 everything is up to date.[/green]")
        return None

    # OK, this push will upload bytes. Run the events tail now to capture
    # the cursor + git/sessions snapshots, then re-walk mm-events to fold
    # the just-written event row into local_manifest. dry_run still gates
    # the tail's own writes; the re-walk reads existing on-disk state.
    events_degradations = events_tail._run_events_tail(
        config, sources, device_id, dry_run=dry_run, quiet=quiet
    )
    if not dry_run:
        mm_internal_cfgs = [s for s in sources if s["name"] in MM_INTERNAL_SOURCE_NAMES]
        if mm_internal_cfgs:
            events_manifest = build_manifest_v2(
                device_id, device_name, mm_internal_cfgs, max_file_size
            )
            local_manifest["sources"].update(events_manifest["sources"])
            # mm-events file count may have rolled (new daily file); regenerate.
            local_manifest["tombstones"] = generate_tombstones(
                local_manifest, remote_manifest, device_id
            )

    # Diff and upload per-source.
    total_bytes = 0
    total_new = 0
    total_modified = 0
    total_deleted = 0

    for src_name, src_data, _remote_src, diff in iter_source_diffs(
        local_manifest, remote_sources, skip_unchanged=True
    ):
        if dry_run:
            if not quiet:
                console.print(f"\n[bold]Source '{src_name}':[/bold]")
                _print_diff_summary(diff, 0)
            continue

        to_upload = {**diff.new, **diff.modified}
        base_path = Path(src_data["base_path"])

        if verbose and not quiet:
            console.print(f"\n[bold]Uploading {len(to_upload)} files from '{src_name}'...[/bold]")

        total_bytes += _upload_changed_blobs(
            backend,
            base_path,
            to_upload,
            device_id,
            passphrase,
            memory_kb,
            verbose=(verbose and not quiet),
            src_name=src_name,
        )
        total_new += len(diff.new)
        total_modified += len(diff.modified)
        total_deleted += len(diff.deleted)

    if dry_run:
        if not quiet:
            elapsed = time.time() - start
            if has_mtime_only and not (total_new or total_modified or total_deleted):
                # Symmetric with the status command's metadata-only branch (v0.12.6).
                # Without this, dry-run would print "Dry run complete" with zero
                # per-source output and the user would miss that `mm push` would
                # republish the manifest to propagate bumped mtimes. NOTE: dry-run
                # counters stay zero (the per-source loop `continue`s before
                # incrementing), so we can't honestly promise "no blobs" -- the
                # events tail may still emit an mm-events row on the actual push.
                console.print(
                    "\n[bold]Would refresh manifest[/bold] (metadata-only changes pending)."
                )
            console.print("\n[bold]Dry run complete.[/bold]")
            console.print(f"  Completed in {elapsed:.1f}s")
        return None

    # Upload manifest (includes tombstones)
    if not quiet:
        if recovering_from_corrupt and not (total_new or total_modified or total_deleted):
            console.print("\n[bold]Rewriting manifest to heal remote corruption...[/bold]")
        elif has_mtime_only and not (total_new or total_modified or total_deleted):
            console.print("\n[bold]Refreshing manifest (metadata-only changes)...[/bold]")
        else:
            console.print(f"\n[bold]Uploading {total_new + total_modified} files...[/bold]")
    manifest_data = serialize_manifest(local_manifest)
    enc_manifest = encrypt(manifest_data, passphrase, memory_kb)
    mkey = manifest_key(device_id)
    backend.put(mkey, enc_manifest)

    # Write sidecar (best-effort: failure warns but does not abort push;
    # the remote manifest succeeded, so peers still have a path to recovery).
    # Warn on both paths: without the sidecar, future corruption recovery
    # on THIS device can only rely on peers -- silently degrading that path
    # defeats the TODOS #1 guarantee.
    try:
        sidecar.write(local_manifest)
    except (OSError, StorageError) as e:
        if quiet:
            print(
                f"mm: warning: failed to write recovery sidecar "
                f"({sidecar.sidecar_path()}): {e}. Push succeeded; future "
                f"corruption recovery will fall back to peer devices.",
                file=sys.stderr,
            )
        else:
            console.print(
                f"[yellow]Warning:[/yellow] failed to write recovery sidecar "
                f"({sidecar.sidecar_path()}): {e}. Push succeeded; future "
                f"corruption recovery will fall back to peer devices."
            )

    # Update device last_seen
    update_last_seen(backend, device_id)

    # Clean up conflict copies (write-path only). Validator gates deletion
    # so a bogus sibling whose name matches the iCloud pattern but doesn't
    # deserialize as a manifest is left on disk with a stderr warning.
    _cleanup_conflict_copies(backend, device_id, passphrase, memory_kb)

    elapsed = time.time() - start
    result = PushResult(
        total_new=total_new,
        total_modified=total_modified,
        total_deleted=total_deleted,
        bytes_transferred=total_bytes,
        elapsed=elapsed,
        events_degradations=events_degradations,
    )

    if not quiet:
        console.print("\n[bold green]Push complete.[/bold green]")
        if total_new:
            console.print(f"  [green]+ {total_new} new[/green]")
        if total_modified:
            console.print(f"  [yellow]~ {total_modified} modified[/yellow]")
        if total_deleted:
            console.print(f"  [red]- {total_deleted} deleted[/red]")
        console.print(f"  Completed in {elapsed:.1f}s")
        if total_bytes:
            mb = total_bytes / (1024 * 1024)
            console.print(f"  {mb:.1f}MB transferred")

    return result


# ── pull ──────────────────────────────────────────────────────────────


ConflictMode = Literal["prompt", "keep-both", "fail"]


@app.command()
def pull(
    from_device: str | None = typer.Option(None, "--from", help="Pull from a specific device ID"),
    source: str | None = typer.Option(
        None, "--source", help="Only pull a specific source (e.g., 'claude', 'gstack')"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    conflict_mode: ConflictMode = typer.Option(
        "keep-both",
        "--conflict-mode",
        help=(
            "How to handle conflicts (local edited, remote differs). "
            "'keep-both' (default): local stays at canonical, remote saved as "
            ".sync-conflict-*. 'prompt': ask per-file. 'fail': preflight "
            "all files and exit 3 (no writes) if any would conflict -- for CI."
        ),
        case_sensitive=False,
    ),
) -> None:
    """Pull session data from storage to local.

    Conflicts (local edited, remote differs) resolve per `--conflict-mode`:
    - keep-both (default): local stays at canonical, remote saved as
      .sync-conflict-*. Files preserved either way.
    - prompt: interactively pick per file at pull time.
    - fail: preflight all files via `_predict_pull_outcome`; if any would
      conflict, print them and exit 3 with no writes (best-effort: a file
      edited between preflight and apply may still produce a .sync-conflict-*,
      re-run pull to surface it). For CI use.

    Exit codes: 0 success, 1 internal error, 2 usage error (typer default),
    3 --conflict-mode fail found conflicts. Exit 3 was chosen (not 2) so CI
    scripts can distinguish "broken invocation" from "conflict refusal" --
    the removal of --no-prompt / --resolve-interactive would otherwise cause
    stale scripts to hit usage-error exit 2 and be misclassified as conflicts.
    """
    config = _get_config()
    _maybe_prompt_migration(config)
    # Re-load in case the migration prompt mutated config on disk so this
    # pull sees the new exclude_patterns.
    config = _get_config()
    passphrase = _get_passphrase_or_exit()

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    try:
        backend = get_backend(config)
        try:
            memory_kb = _init_crypto_session(backend, passphrase, config)
        except MindMeldError as e:
            _error(str(e))
        _pull_core(
            config,
            passphrase,
            memory_kb,
            from_device,
            source,
            verbose,
            dry_run,
            conflict_mode=conflict_mode,
        )
    finally:
        release_lock()

    # Seam 2 — interactive pull tail nudge. Runs AFTER the lock is released
    # so the cold-cache HTTP fetch never blocks pull progress.
    upgrade.emit_nudge_if_due(config)


@dataclass
class _CorruptPeer:
    device_id: str
    device_name: str


@dataclass
class _PredictedConflict:
    device_name: str
    src_name: str
    rel_path: str


@dataclass
class _FsyncWarning:
    parent_dir: Path
    error: str


@dataclass
class _UnknownSourceWarning:
    src_name: str
    device_name: str


@dataclass
class _PerSourceResult:
    """Result of pulling one source from one peer.

    Helpers return this; the main loop aggregates into PullResult and
    passes the full list to _print_pull_summary for per-source line
    rendering.
    """

    src_name: str
    device_name: str
    device_id: str
    outcomes: dict[ApplyOutcome, list[str]]
    bytes_transferred: int
    touched_parents: set[Path]
    # Non-empty only in dry-run mode; holds the diff for _print_pull_prediction.
    dry_run_diff: DiffResult | None = None
    # Set when src_cfg["type"] == "claude" to trigger write_sync_log in the
    # caller. Keyed off type (not name) so a user renaming their claude
    # source to "my-claude" still gets sync-log entries written — otherwise
    # the rename silently breaks project-level activity logging.
    #
    # Scope: this fix covers SAME-DEVICE rename only. Manifests are still
    # keyed by src_name (see manifest.py), so if device A renames "claude"
    # → "work-claude" but device B keeps "claude", B's pull skips A's
    # remote source entirely (unknown-source warning path). Cross-device
    # rename drift is a bigger design question tracked as a known
    # limitation; this flag just makes sure single-device rename doesn't
    # silently break the per-project sync log.
    claude_sync_base: str | None = None

    @property
    def had_changes(self) -> bool:
        """True if ANY file was PROCESSED (written/merged/skipped/conflicted/failed).

        Excludes "unchanged" deliberately. "unchanged" means the apply-time
        re-read matched remote — nothing happened. Treating unchanged-only as
        "had changes" would trigger _cleanup_conflict_copies, which deletes
        valid iCloud conflict copies of the remote manifest. If we recovered
        from a corrupt canonical manifest via a valid conflict copy, that
        deletion leaves only the corrupt canonical → permanent corruption.

        Includes skipped/failed intentionally: one-way-sync setups (always
        local-newer = all skipped) still need cleanup to run or iCloud dup
        files accumulate forever.
        """
        return any(self.outcomes[k] for k in self.outcomes if k != "unchanged")


def _check_fleet_version_or_refuse(backend: LocalBackend, my_device_id: str) -> None:
    """Refuse pull if any peer's last_seen_version is pre-v0.9.2 OR the
    peer's device.json is corrupt/shape-invalid.

    Track 5E (v0.9.2 BREAKING): the conflict-direction inversion ships
    incompatibly. A pre-v0.9.2 peer pushing now would produce conflict
    files under the OLD direction (canonical = remote, sidecar = local),
    but we dispatch by filename prefix (`v0-` = pre-inversion, no prefix
    = post-inversion) and a peer's just-produced (un-prefixed) old-
    direction file would be silently mis-resolved as post-inversion.

    Per-peer classification:
      * safe — last_seen_version is parseable and >= INVERSION_MIN_VERSION.
      * inactive — last_seen ALSO missing (registered, never pushed). ALLOW
        because the peer has no conflict files for us to misinterpret.
      * pre-v0.9.2 — last_seen present but last_seen_version missing OR
        parses to a version < INVERSION_MIN_VERSION. REFUSE.
      * dropped — device.json corrupt/shape-invalid. REFUSE by storage
        key — we can't read its version, so we can't trust its conflict
        files (codex-2 #3).

    Refusal exits non-zero via `_error` BEFORE any pull I/O. Implementation
    uses the silent `list_devices_with_drops`; the loud-on-stderr
    `_list_devices_warn` runs later in `_select_devices` only if the
    fleet check passes.
    """
    valid, dropped = list_devices_with_drops(backend)
    refusals: list[str] = []

    for key, reason in dropped:
        refusals.append(
            f"  storage key {key} — corrupt device.json ({reason}); can't read last_seen_version"
        )

    try:
        threshold = Version(INVERSION_MIN_VERSION)
    except InvalidVersion:
        # Defensive — INVERSION_MIN_VERSION is hardcoded.
        return

    for d in valid:
        did = d.get("device_id")
        if did == my_device_id:
            continue
        if not d.get("last_seen"):
            # Inactive peer (registered, never pushed). No conflict files
            # exist on storage from this peer — safe to ignore.
            continue
        version_str = d.get("last_seen_version")
        # device_name, did, version_str are peer-controlled — sanitize for
        # the refusal message that flows through Rich console.print.
        safe_dname = safe_str(d.get("device_name", "?"))
        safe_did = safe_str(did)
        if not isinstance(version_str, str) or not version_str:
            refusals.append(
                f"  device {safe_dname} ({safe_did}) — "
                f"last_seen_version missing (last push was on a pre-"
                f"v{INVERSION_MIN_VERSION} mm)"
            )
            continue
        try:
            peer_version = Version(version_str)
        except InvalidVersion:
            refusals.append(
                f"  device {safe_dname} ({safe_did}) — "
                f"last_seen_version={safe_str(version_str)!r} is malformed"
            )
            continue
        if peer_version < threshold:
            refusals.append(
                f"  device {safe_dname} ({safe_did}) — "
                f"last_seen_version={safe_str(version_str)} < {INVERSION_MIN_VERSION}"
            )

    if refusals:
        _error(
            f"Mixed-version fleet detected. v{INVERSION_MIN_VERSION} "
            f"inverted the conflict-file direction (canonical = local, "
            f"sidecar = remote); a pre-v{INVERSION_MIN_VERSION} peer "
            f"pushing now would produce conflict files under the OLD "
            f"semantics that this puller can't safely dispatch.\n\n"
            f"Update the following peer(s) to v{INVERSION_MIN_VERSION} "
            f"and have them push at least once before pulling here:\n"
            + "\n".join(refusals)
            + "\n\nRun `mm devices` for the version table. "
            "Last-resort recovery: hand-edit device.json to add "
            f'"last_seen_version": "{INVERSION_MIN_VERSION}"' + " — only after "
            "verifying the peer is actually upgraded."
        )


def _select_devices(
    backend: LocalBackend, my_device_id: str, from_device: str | None
) -> tuple[list[dict], list[dict]]:
    """Return (all_devices, pull_targets).

    Single call to _list_devices_warn — dedups the double-call bug
    (cli.py:1692 + 1709 pre-decomp) that emitted each dropped-device
    warning twice per pull. One warning per dropped peer per pull is
    the correct semantic.

    `all_devices` is used for tombstone collection (every peer's
    manifest feeds tombstones; we don't pull data from ourselves but
    we DO read our own tombstones via collect_tombstones).

    `pull_targets` is filtered: only the peer `from_device` matches, or
    all peers excluding self.
    """
    all_devices = _list_devices_warn(backend)
    if from_device:
        pull_targets = [d for d in all_devices if d["device_id"] == from_device]
    else:
        pull_targets = [d for d in all_devices if d["device_id"] != my_device_id]
    return all_devices, pull_targets


def _prefetch_manifests(
    backend: LocalBackend, devices: list[dict], passphrase: str, memory_kb: int
) -> tuple[dict[str, dict | None], list[_CorruptPeer]]:
    """Fetch every device's manifest. Missing/corrupt both map to None.

    Returns (cache, corrupt_peers). Callers route corrupt_peers to stderr
    via _print_pull_summary (load-bearing signal: silent skip = partial
    pull that looks like a successful pull).
    """
    cache: dict[str, dict | None] = {}
    corrupt: list[_CorruptPeer] = []
    for d in devices:
        did = d["device_id"]
        peer_fetch = _fetch_remote_manifest(backend, did, passphrase, memory_kb)
        if peer_fetch.status == "corrupt":
            corrupt.append(_CorruptPeer(device_id=did, device_name=d.get("device_name", did)))
        cache[did] = peer_fetch.manifest if peer_fetch.is_ok else None
    return cache, corrupt


def _preflight_conflicts(
    pull_targets: list[dict],
    manifest_cache: dict[str, dict | None],
    local_sources_map: dict[str, dict[str, Any]],
    source_filter: str | None,
    all_tombstones: dict[str, dict[str, str]],
) -> list[_PredictedConflict]:
    """Classify every file preflight; return predicted conflicts.

    Cross-peer simulation: walking peers in iteration order, maintain
    an overlay of (src_name, rel_path) -> predicted-final-sha for files
    preflight said would be cleanly written. When a later peer ships
    the same path, predict against the overlay (what local WILL be
    after the earlier peer's write) rather than the stale on-disk sha.
    Without this, peer A writing Y then peer B writing Z is missed:
    preflight sees empty local for both, predicts clean, apply
    produces a .sync-conflict-* — exactly the "no writes on fail"
    violation the flag prevents.

    Caller (pull_core) exits 3 if the list is non-empty. Race-safe
    only best-effort (TOCTOU between preflight and apply is possible;
    re-run pull to surface late conflicts).
    """
    predicted: list[_PredictedConflict] = []
    overlay: dict[tuple[str, str], str] = {}
    for device in pull_targets:
        dname = device["device_name"]
        remote_manifest = manifest_cache.get(device["device_id"])
        if remote_manifest is None:
            continue
        for src_name, src_data in remote_manifest.get("sources", {}).items():
            if source_filter and src_name != source_filter:
                continue
            if src_name not in local_sources_map:
                continue  # unknown source counted elsewhere, not a conflict
            base_path = local_sources_map[src_name]["path"]
            for rel_path, info in src_data.get("files", {}).items():
                if is_tombstoned(src_name, rel_path, all_tombstones):
                    continue
                overlay_sha = overlay.get((src_name, rel_path))
                if overlay_sha is not None:
                    # An earlier peer already predicted a clean write.
                    # Next peer conflicts iff its sha differs from what
                    # the earlier peer will leave.
                    if overlay_sha != info.get("sha256"):
                        predicted.append(_PredictedConflict(dname, src_name, rel_path))
                    continue
                outcome = _predict_pull_outcome(rel_path, info, base_path)
                if outcome == "conflict":
                    predicted.append(_PredictedConflict(dname, src_name, rel_path))
                elif outcome in ("write", "merge"):
                    overlay[(src_name, rel_path)] = info.get("sha256", "")
    return predicted


def _empty_outcomes() -> dict[ApplyOutcome, list[str]]:
    return {
        "written": [],
        "merged": [],
        "merged-via-lcs": [],
        "skipped": [],
        "conflicted": [],
        "unchanged": [],
        "failed": [],
    }


def _pull_one_source(
    backend: LocalBackend,
    *,
    src_name: str,
    src_type: str,
    src_data: dict,
    did: str,
    dname: str,
    base_path: Path,
    all_tombstones: dict[str, dict[str, str]],
    passphrase: str,
    memory_kb: int,
    interactive_resolve: bool,
    dry_run: bool,
    verbose_console: bool,
    quiet: bool = False,
    devices: list[dict[str, Any]] | None = None,
    pending_inline_bumps: dict[Path, float] | None = None,
) -> _PerSourceResult:
    """Pull one source from one peer. Returns _PerSourceResult.

    `src_type` is the source's `type` field from the local config. Used
    to gate sync-log behavior: only claude-type sources get a per-project
    `.mind-meld-log.md`. Passing type explicitly (rather than re-deriving
    from name) lets users rename the claude source without losing sync-log.

    `verbose_console` is `(verbose and not quiet)` — controls per-file
    console output inside _download_and_apply.

    `quiet` is the autopull flag — gates the Track 5B Task 4 download
    progress widget (silent in autopull, visible otherwise).
    """
    remote_files = src_data.get("files", {})
    base_result = _PerSourceResult(
        src_name=src_name,
        device_name=dname,
        device_id=did,
        outcomes=_empty_outcomes(),
        bytes_transferred=0,
        touched_parents=set(),
    )
    if not remote_files:
        return base_result

    # Build local state for diff. mtime is re-read at apply time so it
    # reflects what the file actually looks like when we act on it.
    local_files: dict[str, dict] = {}
    for rel_path in remote_files:
        local_path = base_path / rel_path
        if local_path.exists():
            try:
                sha = hash_file(local_path)
                local_files[rel_path] = {"sha256": sha}
            except (PermissionError, OSError):
                pass

    # Arg-swap: this is the additive pull path. See diff_files docstring
    # — `new`/`modified` are files to download; `deleted` is ignored.
    diff = diff_files(remote_files, local_files)

    if dry_run:
        base_result.dry_run_diff = diff
        return base_result

    to_download = {**diff.new, **diff.modified}
    to_download = {
        path: info
        for path, info in to_download.items()
        if not is_tombstoned(src_name, path, all_tombstones)
    }
    if not to_download:
        return base_result

    bt, outcomes = _download_and_apply(
        backend,
        base_path,
        to_download,
        did,
        passphrase,
        memory_kb,
        interactive_resolve=interactive_resolve,
        verbose=verbose_console,
        quiet=quiet,
        devices=devices,
        pending_inline_bumps=pending_inline_bumps,
    )

    touched_parents: set[Path] = set()
    for rel in (
        outcomes["written"]
        + outcomes["merged"]
        + outcomes["merged-via-lcs"]
        + outcomes["conflicted"]
    ):
        touched_parents.add((base_path / rel).parent)

    return _PerSourceResult(
        src_name=src_name,
        device_name=dname,
        device_id=did,
        outcomes=outcomes,
        bytes_transferred=bt,
        touched_parents=touched_parents,
        claude_sync_base=str(base_path) if src_type == "claude" else None,
    )


def _fsync_touched_parents(touched_parents: set[Path]) -> list[_FsyncWarning]:
    """Deferred-durability commit: fsync each unique parent directory.

    A failure means some of this pull's renames may be non-durable —
    caller surfaces to stderr but does not roll back (files are already
    in place; a subsequent pull will simply re-apply if needed).
    """
    warnings: list[_FsyncWarning] = []
    for parent_dir in sorted(touched_parents):
        try:
            fsutil.fsync_dir(parent_dir)
        except StorageError as e:
            warnings.append(_FsyncWarning(parent_dir=parent_dir, error=str(e)))
    return warnings


def _print_preflight_conflicts(predicted: list[_PredictedConflict], quiet: bool) -> None:
    """Print predicted conflicts before --conflict-mode=fail raises.

    Quiet (autopull): one-liner per conflict to stderr.
    Non-quiet: rich console with resolution hint.
    """
    # src_name, rel_path, device_name are all peer-controlled — sanitize.
    if quiet:
        for p in predicted:
            print(
                f"mm: conflict {safe_str(p.src_name)}/{safe_str(p.rel_path)} "
                f"(from {safe_str(p.device_name)})",
                file=sys.stderr,
            )
        return
    console.print(f"[red]Pull refused:[/red] {len(predicted)} file(s) would conflict.")
    for p in predicted:
        console.print(
            f"  [yellow]! conflict[/yellow] {safe_str(p.src_name)}/"
            f"{safe_str(p.rel_path)} (from {safe_str(p.device_name)})"
        )
    console.print(
        "\nResolve conflicts locally, or re-run with --conflict-mode keep-both to auto-rename."
    )


# Inline-path display cap for the non-verbose pull summary. 20 was picked
# from the 2026-04-24 first-pull session (286-file pull / 6 conflicts):
# enough to surface a typical conflict batch without spamming, and short
# enough that --verbose still feels distinct.
_INLINE_PATH_CAP = 20


def _format_inline_paths(paths: list[str], *, verbose: bool, sep: str) -> str:
    """Format an inline path list for the quiet-mode stderr summary.

    Caps to _INLINE_PATH_CAP unless verbose. Cap overflow renders as
    "(and N more)" suffix so users know they're seeing a slice.
    """
    if verbose or len(paths) <= _INLINE_PATH_CAP:
        return sep.join(paths)
    shown = sep.join(paths[:_INLINE_PATH_CAP])
    return f"{shown} (and {len(paths) - _INLINE_PATH_CAP} more)"


def _print_inline_paths(paths: list[str], *, verbose: bool, color: str) -> None:
    """Render an inline path list under a non-quiet per-source summary line.

    Uses 4-space indent so the device→source→file hierarchy stays visible
    when multiple devices share a source name. Caps to _INLINE_PATH_CAP
    unless --verbose; overflow renders as a dim "... and N more" line.
    """
    cap = len(paths) if verbose else min(_INLINE_PATH_CAP, len(paths))
    for p in paths[:cap]:
        console.print(f"    [{color}]- {p}[/{color}]")
    if not verbose and len(paths) > cap:
        console.print(f"    [dim]... and {len(paths) - cap} more[/dim]")


def _print_pull_summary(
    result: PullResult,
    corrupt_peers: list[_CorruptPeer],
    unknown_sources: list[_UnknownSourceWarning],
    fsync_warnings: list[_FsyncWarning],
    per_source_results: list[_PerSourceResult],
    quiet: bool,
    verbose: bool,
) -> None:
    """Single I/O owner for pull output.

    Load-bearing warnings (corrupt peers, unknown sources, fsync
    failures, per-source conflicts/failures) ALWAYS to stderr — they
    survive quiet mode because silent suppression would mask
    data-at-risk conditions (see CLAUDE.md "Load-bearing warnings").

    Cosmetic summary (totals, bytes transferred, elapsed, per-source
    verbose lines) goes to console only when !quiet.
    """
    # Load-bearing: corrupt peers (silent skip = partial pull masquerading
    # as successful pull). device_name and device_id are peer-controlled —
    # sanitize before render (Group 7 preflight #1 sweep extension).
    for peer in corrupt_peers:
        msg = (
            f"manifest for device {safe_str(peer.device_name)} "
            f"({safe_str(peer.device_id)}) "
            f"is corrupt - skipping pull from this device."
        )
        if quiet:
            print(f"mm: warning: {msg}", file=sys.stderr)
        else:
            console.print(f"[yellow]Warning:[/yellow] {msg}")

    # Load-bearing: unknown sources (partition risk — rename drift or
    # missed config migration). src_name and device_name are peer-controlled.
    for unk in unknown_sources:
        msg = (
            f"skipping unknown source '{safe_str(unk.src_name)}' from "
            f"{safe_str(unk.device_name)} - not configured locally"
        )
        if quiet:
            print(f"mm: warning: {msg}", file=sys.stderr)
        else:
            console.print(f"  [yellow]Warning:[/yellow] {msg}")

    # Load-bearing: fsync failures (pulls non-durable).
    for w in fsync_warnings:
        msg = f"durability fsync failed on {safe_str(w.parent_dir)} — {safe_str(w.error)}"
        if quiet:
            print(f"mm: warning: {msg}", file=sys.stderr)
        else:
            console.print(f"  [yellow]warning:[/yellow] {msg}")

    # Load-bearing: per-source conflicts/failures (D11 contract fix).
    # The docstring's promise that these reach stderr in quiet mode was
    # never honored before Track 5B — the early `return` below ate them.
    # Quiet path now emits one stderr line per per-source category, with
    # `<device>/<source>` prefix because the per-device header (printed
    # at the call site, not here) is suppressed in quiet mode.
    if quiet:
        for r in per_source_results:
            src_conflicted = len(r.outcomes["conflicted"])
            src_failed = len(r.outcomes["failed"])
            if src_conflicted:
                paths = _format_inline_paths(r.outcomes["conflicted"], verbose=verbose, sep=", ")
                print(
                    f"mm: warning: {r.device_name}/{r.src_name} — "
                    f"{src_conflicted} conflicts: {paths}",
                    file=sys.stderr,
                )
            if src_failed:
                paths = _format_inline_paths(r.outcomes["failed"], verbose=verbose, sep=", ")
                print(
                    f"mm: warning: {r.device_name}/{r.src_name} — {src_failed} failed: {paths}",
                    file=sys.stderr,
                )
        return

    # Per-source lines (conflicts/failures always; verbose otherwise).
    for r in per_source_results:
        src_written = len(r.outcomes["written"])
        src_merged = len(r.outcomes["merged"]) + len(r.outcomes["merged-via-lcs"])
        src_skipped = len(r.outcomes["skipped"])
        src_conflicted = len(r.outcomes["conflicted"])
        src_failed = len(r.outcomes["failed"])
        if src_conflicted or src_failed or verbose:
            line = f"  [bold]{r.src_name}:[/bold]"
            if src_written:
                line += f" [green]{src_written} written[/green]"
            if src_merged:
                line += f" [cyan]{src_merged} merged[/cyan]"
            if src_skipped:
                line += f" [dim]{src_skipped} skipped (local newer)[/dim]"
            if src_conflicted:
                line += f" [yellow]{src_conflicted} conflicts[/yellow]"
            if src_failed:
                line += f" [red]{src_failed} failed[/red]"
            console.print(line)
            # Task 2: inline conflicted/failed path list under per-source
            # line. 4-space indent keeps the device→source→file hierarchy
            # readable when multiple devices share a source name (D10).
            # --verbose unlocks the cap (D5).
            if src_conflicted:
                _print_inline_paths(r.outcomes["conflicted"], verbose=verbose, color="yellow")
            if src_failed:
                _print_inline_paths(r.outcomes["failed"], verbose=verbose, color="red")

    # Totals.
    console.print("\n[bold green]Pull complete.[/bold green]")
    parts = []
    if result.total_written:
        parts.append(f"{result.total_written} written")
    if result.total_merged:
        parts.append(f"{result.total_merged} merged")
    if result.total_skipped:
        parts.append(f"{result.total_skipped} skipped (local newer)")
    if result.total_conflicted:
        parts.append(f"[yellow]{result.total_conflicted} conflicts[/yellow]")
    if result.total_failed:
        parts.append(f"[red]{result.total_failed} failed[/red]")
    if result.total_skipped_unknown_source:
        parts.append(
            f"[yellow]{result.total_skipped_unknown_source} unknown source(s) skipped[/yellow]"
        )
    if parts:
        console.print("  " + ", ".join(parts))
    else:
        console.print("  nothing to apply")
    if result.bytes_transferred:
        mb = result.bytes_transferred / (1024 * 1024)
        console.print(f"  {mb:.1f}MB transferred")
    console.print(f"  Completed in {result.elapsed:.1f}s")
    if result.total_conflicted:
        console.print(
            "  [yellow]Run [bold]mm conflicts[/bold] to review, "
            "[bold]mm resolve[/bold] to pick a winner.[/yellow]"
        )


# ── iter_source_diffs: shared push/status/diff iteration ──────────────
#
# Three call sites (_push_core, status, diff) shared a 3-line per-source
# boilerplate: fetch remote_src, run diff_files, optionally filter by
# --source / skip unchanged. Extracted here. The pull path cannot use
# this helper — it calls diff_files(remote, local) with arguments swapped
# (see diff_files docstring's "Arg-swap convention" note), so the
# semantics of .new/.modified/.deleted flip and a shared iterator would
# obscure that. Pull stays inline.
#
def iter_source_diffs(
    local_manifest: dict,
    remote_sources: dict,
    *,
    source_filter: str | None = None,
    skip_unchanged: bool = False,
) -> Iterator[tuple[str, dict, dict, DiffResult]]:
    """Yield (src_name, src_data, remote_src, diff) per local source.

    Args:
      local_manifest: local manifest v2 with a "sources" dict.
      remote_sources: remote manifest's sources dict (`{}` if no remote).
      source_filter: if set, yield only this one source.
      skip_unchanged: if True, skip sources where `diff.has_changes` is False.
    """
    for src_name, src_data in local_manifest["sources"].items():
        if source_filter is not None and src_name != source_filter:
            continue
        remote_src = remote_sources.get(src_name, {"files": {}})
        diff = diff_files(src_data["files"], remote_src.get("files", {}))
        if skip_unchanged and not diff.has_changes:
            continue
        yield src_name, src_data, remote_src, diff


# ── _pull_core: decomposition pattern ──────────────────────────────────
#
# _pull_core follows a "helpers return data, _print_pull_summary owns
# user-visible output" pattern. Each helper performs its own storage /
# filesystem I/O but does NOT call console.print. Load-bearing warnings
# (corrupt peers, unknown sources, fsync failures, per-source
# conflicts/failures) are accumulated into lists and routed to stderr by
# _print_pull_summary — they survive quiet-mode suppression.
#
# The rest of cli.py (push, status, diag, recover) still uses the older
# side-effect-during-logic style. Migrate opportunistically when touching.
#
def _pull_core(
    config: dict,
    passphrase: str,
    memory_kb: int,
    from_device: str | None = None,
    source_filter: str | None = None,
    verbose: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
    conflict_mode: ConflictMode = "keep-both",
) -> PullResult:
    """Core pull logic shared by pull and autopull.

    Additive-only: downloads new and modified files, never deletes local
    files. Tombstoned files are skipped. JSONL files are merged. MEMORY.md
    files are line-merged. Non-mergeable files with divergent local edits
    follow the _apply_incoming_file decision tree.

    `conflict_mode`:
      - "keep-both": default; auto keep-both on conflict.
      - "prompt":    ask per-file (interactive only).
      - "fail":      preflight every file; if any would conflict, raise
                     typer.Exit(3) with no writes. Best-effort (TOCTOU).

    When quiet=True, load-bearing warnings still reach stderr; cosmetic
    progress chatter is suppressed.
    """
    interactive_resolve_flag = conflict_mode == "prompt"
    start = time.time()
    my_device_id = config["device"]["id"]
    backend = get_backend(config)

    # Track 12A: shared accumulator for inline keep-canonical decisions, drained
    # once after the whole device loop. Only allocated in interactive (`prompt`)
    # mode — non-interactive pulls (default keep-both, autopull) never populate
    # it, so None keeps the bump machinery a no-op down the whole call chain.
    pending_inline_bumps: dict[Path, float] | None = {} if interactive_resolve_flag else None

    # Track 5E gate: refuse pull if any peer is pre-v0.9.2 OR has a
    # corrupt device.json (we can't read its version). Runs BEFORE the
    # pre-inversion migration sweep so we don't accidentally migrate
    # files in a refusal scenario where the user is about to upgrade
    # their peers and re-pull. Exits via _error → typer.Exit(1).
    _check_fleet_version_or_refuse(backend, my_device_id)

    # Pre-inversion conflict-file migration. Runs once per pull under the
    # already-held mm lockfile — safe against autopull racing with another
    # discovery walk. mm pull is about to potentially produce NEW (post-
    # inversion) conflict files via `_apply_conflict`; without this sweep,
    # the user's tree would end up with a mix of pre-inversion and post-
    # inversion files indistinguishable except by mtime — and resolve's
    # dual-mode dispatch needs the prefix, not the timestamp.
    resolveflow._find_conflict_files(config, migrate_pre_inversion=True)

    # Widened to carry path + type per source. Type is load-bearing for
    # the sync-log gate in _pull_one_source — keying on type (not name)
    # lets users rename the claude source without losing per-project logs.
    local_sources_map: dict[str, dict[str, Any]] = {
        src_cfg["name"]: {
            "path": Path(src_cfg["path"]).expanduser().resolve(),
            "type": src_cfg["type"],
        }
        for src_cfg in get_sources(config)
    }

    all_devices, pull_targets = _select_devices(backend, my_device_id, from_device)
    if from_device and not pull_targets:
        if not quiet:
            _error(f"Device not found: {from_device}")
    if not pull_targets:
        if not quiet:
            console.print("[yellow]No other devices found to pull from.[/yellow]")
        return PullResult(elapsed=time.time() - start)

    manifest_cache, corrupt_peers = _prefetch_manifests(backend, all_devices, passphrase, memory_kb)

    # Consumer-boundary exclude_patterns filter for pull. Drops excluded
    # paths from each peer manifest before tombstone collection AND before
    # the per-source download loop. Without this, a peer who hasn't yet
    # adopted the local exclude_patterns ships an excluded file in their
    # manifest, and pull would either download it (defeating the exclude)
    # or surface it as a conflict. Filter HERE, not in
    # `_fetch_remote_manifest` — `mm gc` reads raw manifests via that path
    # to compute referenced blobs, and a filtered manifest there would
    # mark live peer blobs as orphans (codex-2 #1).
    # Same disable-then-exclude order as _push_core. The disabled-sources
    # filter strips entire `sources.<name>` entries so the per-peer
    # download loop never tries to land a disabled source's files locally.
    # P0 invariant — see _filter_disabled_sources docstring.
    exclude_map, skip_prefixes = _build_exclude_map(config)
    disabled_sources = list(config.get("sync", {}).get("disabled_sources", []) or [])
    if disabled_sources:
        manifest_cache = {
            did: (None if m is None else _filter_disabled_sources(m, disabled_sources))
            for did, m in manifest_cache.items()
        }
    # Always run: conflict-shaped names and `.extend-root` are stripped even
    # when no source declares exclude_patterns (claude-only installs).
    filtered_cache: dict[str, dict | None] = {}
    for did, m in manifest_cache.items():
        if m is None:
            filtered_cache[did] = None
            continue
        filtered = _filter_excluded_paths(m, exclude_map, skip_prefixes)
        # Log "excluded" per peer-path so `mm log --action excluded`
        # shows what the user is NOT pulling because of their config.
        #
        # 5E ship-fix: skip logging in quiet (autopull/autopush) mode.
        # Without this gate, autopull writes one record per peer ×
        # source × excluded-path tuple on every hook fire — at
        # ~100 projects × hourly pulls, the 1MB pullhistory cap
        # rotates within hours and evicts real `written / merged /
        # conflicted / failed` records to `.1`. The forensic-aid
        # contract becomes useless. Interactive `mm pull` still
        # logs the full set so users can audit their excludes.
        if not quiet:
            for src_name, src_data in m.get("sources", {}).items():
                kept = filtered.get("sources", {}).get(src_name, {}).get("files", {})
                for rel_path, info in src_data.get("files", {}).items():
                    if rel_path not in kept:
                        pullhistory.append(
                            verb="pull",
                            device=did,
                            source=src_name,
                            rel_path=rel_path,
                            action="excluded",
                            remote_sha=info.get("sha256"),
                        )
        filtered_cache[did] = filtered
    manifest_cache = filtered_cache

    # Group 7 preflight #6 + D11: pull-time case-collision detection.
    # On case-insensitive local FS (APFS default, NTFS), two manifest
    # entries that differ only in casing (peer A "Projects/x.md" + peer B
    # "projects/x.md") would resolve to the same inode locally — the
    # second would silently overwrite or alias the first. Detect per
    # source, emit `mm: warning:` per cluster, drop all-but-lex-first
    # from each peer manifest. Codex outside-voice T5: a Linux peer can
    # legitimately have both names; we don't normalize manifest keys
    # globally — we only skip the duplicate WRITE on case-insensitive
    # consumers. The raw manifest stays intact for future cross-platform
    # peers (mm gc reads via _fetch_remote_manifest, unfiltered).
    case_collisions = _detect_pull_case_collisions(manifest_cache, local_sources_map)
    if case_collisions:
        for src_name, clusters in case_collisions.items():
            for paths in clusters.values():
                # src_name and paths come from peer manifests — sanitize
                # before render (Group 7 preflight #1 sweep extension).
                safe_kept = safe_str(paths[0])
                safe_dropped = ", ".join(safe_str(p) for p in paths[1:])
                stderr_console.print(
                    f"mm: warning: case-collision in source "
                    f"'{safe_str(src_name)}' on case-insensitive FS — "
                    f"keeping '{safe_kept}', skipping [{safe_dropped}] "
                    f"(Linux peer can legitimately push both; this device "
                    f"can only represent one)"
                )
        manifest_cache = _drop_case_collisions_from_manifests(manifest_cache, case_collisions)

    all_tombstones = collect_tombstones(
        list(manifest_cache.keys()),
        lambda did: manifest_cache.get(did),
    )

    if conflict_mode == "fail":
        predicted = _preflight_conflicts(
            pull_targets,
            manifest_cache,
            local_sources_map,
            source_filter,
            all_tombstones,
        )
        if predicted:
            _print_preflight_conflicts(predicted, quiet)
            raise typer.Exit(3)

    # Aggregate accumulators. Load-bearing warnings go through lists,
    # routed to stderr by _print_pull_summary. The try/finally below
    # guarantees _print_pull_summary runs even on unexpected exceptions
    # so accumulated warnings (corrupt peers, unknown sources, fsync
    # failures) still reach stderr — preserving the v0.8.1 visible-
    # failure contract.
    per_source_results: list[_PerSourceResult] = []
    unknown_sources: list[_UnknownSourceWarning] = []
    touched_parents: set[Path] = set()
    device_names: list[str] = []
    fsync_warnings: list[_FsyncWarning] = []
    total_written = total_merged = total_skipped = total_conflicted = 0
    total_failed = total_skipped_unknown_source = bytes_transferred = 0

    try:
        for device in pull_targets:
            did = device["device_id"]
            dname = device["device_name"]
            if not quiet:
                console.print(f"\n[bold]Pulling from {safe_str(dname)} ({safe_str(did)})...[/bold]")

            remote_manifest = manifest_cache.get(did)
            if remote_manifest is None:
                if not quiet:
                    console.print(f"  [yellow]No manifest for {safe_str(dname)}[/yellow]")
                continue

            device_had_changes = False

            for src_name, src_data in remote_manifest.get("sources", {}).items():
                if source_filter and src_name != source_filter:
                    continue

                if src_name not in local_sources_map:
                    total_skipped_unknown_source += 1
                    unknown_sources.append(
                        _UnknownSourceWarning(src_name=src_name, device_name=dname)
                    )
                    continue

                src_info = local_sources_map[src_name]
                base_path = src_info["path"]
                src_type = src_info["type"]
                if verbose and not quiet:
                    console.print(
                        f"  [bold]Source '{safe_str(src_name)}' ({safe_str(base_path)}):[/bold]"
                    )

                per_source = _pull_one_source(
                    backend,
                    src_name=src_name,
                    src_type=src_type,
                    src_data=src_data,
                    did=did,
                    dname=dname,
                    base_path=base_path,
                    all_tombstones=all_tombstones,
                    passphrase=passphrase,
                    memory_kb=memory_kb,
                    interactive_resolve=interactive_resolve_flag,
                    dry_run=dry_run,
                    verbose_console=(verbose and not quiet),
                    quiet=quiet,
                    devices=all_devices,
                    pending_inline_bumps=pending_inline_bumps,
                )

                if dry_run and per_source.dry_run_diff is not None:
                    if not quiet:
                        console.print(f"  Dry run for {safe_str(dname)}/{safe_str(src_name)}:")
                        _print_pull_prediction(per_source.dry_run_diff, base_path, src_name)
                    continue

                if not per_source.had_changes:
                    if verbose and not quiet:
                        console.print(
                            f"  [green]Up to date with "
                            f"{safe_str(dname)}/{safe_str(src_name)}.[/green]"
                        )
                    continue

                per_source_results.append(per_source)
                bytes_transferred += per_source.bytes_transferred
                touched_parents |= per_source.touched_parents
                total_written += len(per_source.outcomes["written"])
                total_merged += len(per_source.outcomes["merged"]) + len(
                    per_source.outcomes["merged-via-lcs"]
                )
                total_skipped += len(per_source.outcomes["skipped"])
                total_conflicted += len(per_source.outcomes["conflicted"])
                total_failed += len(per_source.outcomes["failed"])
                device_had_changes = True

                # Log per-file outcomes for `mm log` audit trail. "unchanged"
                # is intentionally omitted — it represents apply-time
                # convergence (no I/O), and logging it would dwarf the
                # forensic signal in the file. All other outcomes ARE
                # I/O events worth recording.
                for action_key in (
                    "written",
                    "merged",
                    "merged-via-lcs",
                    "skipped",
                    "conflicted",
                    "failed",
                ):
                    for rel_path in per_source.outcomes.get(action_key, []):
                        remote_info = src_data.get("files", {}).get(rel_path, {})
                        pullhistory.append(
                            verb="pull",
                            device=did,
                            source=src_name,
                            rel_path=rel_path,
                            action=action_key,
                            remote_sha=remote_info.get("sha256"),
                        )

                # Claude sync log is best-effort: log file is cosmetic
                # signal for Claude Code, losing it on error is harmless.
                # Swallowing the exception here protects the accumulated
                # corrupt-peer / unknown-source warnings from being lost
                # if write_sync_log raises.
                if per_source.claude_sync_base is not None:
                    try:
                        logs = write_sync_log(
                            claude_base=per_source.claude_sync_base,
                            device_name=dname,
                            device_id=did,
                            new_files=per_source.outcomes["written"],
                            modified_files=(
                                per_source.outcomes["merged"]
                                + per_source.outcomes["merged-via-lcs"]
                            ),
                            deleted_files=[],
                            conflicted_files=per_source.outcomes["conflicted"],
                            skipped_files=per_source.outcomes["skipped"],
                        )
                    except (OSError, StorageError) as e:
                        msg = f"sync log write failed: {e}"
                        if quiet:
                            print(f"mm: warning: {msg}", file=sys.stderr)
                        else:
                            console.print(f"  [yellow]warning:[/yellow] {msg}")
                    else:
                        if verbose and not quiet and logs:
                            for log in logs:
                                console.print(f"  [dim]wrote sync log: {log}[/dim]")

            if device_had_changes:
                device_names.append(dname)
                # Clean up iCloud/Dropbox manifest conflict copies is best-
                # effort: a failure here just leaves dup conflict copies on
                # disk for the next pull to retry. Swallowing protects
                # accumulated warnings from the visible-failure contract.
                try:
                    _cleanup_conflict_copies(backend, did, passphrase, memory_kb)
                except (OSError, StorageError) as e:
                    msg = f"manifest conflict-copy cleanup failed for {dname}: {e}"
                    if quiet:
                        print(f"mm: warning: {msg}", file=sys.stderr)
                    else:
                        console.print(f"  [yellow]warning:[/yellow] {msg}")

        fsync_warnings = _fsync_touched_parents(touched_parents)

        # Track 12A: end-of-pull-batch drain. Every peer has been walked, so
        # the recorded bump value beats all of them at once. INSIDE the try:
        # a typer.Abort() from the inline (a)bort choice propagates past this
        # point straight to `finally`, intentionally skipping the drain —
        # abort means the user does not trust this pull, so half-made
        # keep-canonical decisions are not broadcast to the fleet.
        _drain_inline_bumps(pending_inline_bumps)
    finally:
        # Even if an unexpected exception propagates from the loop above,
        # emit accumulated load-bearing warnings to stderr. The v0.8.1
        # contract requires corrupt-peer / unknown-source / fsync-failure
        # warnings to survive quiet mode AND partial pulls.
        _partial_result = PullResult(
            total_written=total_written,
            total_merged=total_merged,
            total_skipped=total_skipped,
            total_conflicted=total_conflicted,
            total_failed=total_failed,
            total_skipped_unknown_source=total_skipped_unknown_source,
            bytes_transferred=bytes_transferred,
            device_names=device_names,
            elapsed=time.time() - start,
            durability_fsync_failures=len(fsync_warnings),
            corrupt_peer_count=len(corrupt_peers),
        )
        _print_pull_summary(
            _partial_result,
            corrupt_peers=corrupt_peers,
            unknown_sources=unknown_sources,
            fsync_warnings=fsync_warnings,
            per_source_results=per_source_results,
            quiet=quiet,
            verbose=verbose,
        )

    return _partial_result


# ── status ────────────────────────────────────────────────────────────


_BROKEN_DIAG_STATUS_TO_INSTALL = {
    "foreign-dangling": "foreign",
    "error": "failed",
}


def _render_broken_skill_status(row: dict) -> str:
    """Render the first broken ``mm diag`` row through ``render_skill_status``."""
    key = row.get("key")
    if not isinstance(key, str) or not key:
        return safe_str(str(row.get("agent", "")))
    try:
        descriptor = skill_link._descriptor_for(key)
    except KeyError:
        return safe_str(str(row.get("agent", "") or key))
    raw_status = str(row.get("status") or "failed")
    mapped = _BROKEN_DIAG_STATUS_TO_INSTALL.get(raw_status, raw_status)
    store = row.get("store")
    result = skill_link.SkillInstallResult(
        descriptor,
        mapped,  # type: ignore[arg-type]
        link_target=Path(store) if isinstance(store, str) and store else None,
        reason=str(row["detail"]) if row.get("detail") else None,
    )
    return skill_link.render_skill_status(result)


@app.command()
def status(
    source: str | None = typer.Option(
        None, "--source", help="Show status for a specific source only"
    ),
) -> None:
    """Show sync status: local vs remote state."""
    config = _get_config()
    passphrase = _get_passphrase_or_exit()
    device_id = config["device"]["id"]
    device_name = config["device"]["name"]
    max_file_size = config["sync"]["max_file_size"]

    backend = get_backend(config)
    try:
        memory_kb = _init_crypto_session(backend, passphrase, config)
    except MindMeldError as e:
        _error(str(e))

    # Build local manifest (v2)
    sources_configs = get_sources(config)
    local_manifest = build_manifest_v2(device_id, device_name, sources_configs, max_file_size)

    # Fetch remote manifest (tri-state — surface missing/corrupt to user).
    # fetch.manifest is pre-normalized via load_manifest.
    fetch = _fetch_remote_manifest(backend, device_id, passphrase, memory_kb)
    remote_manifest = fetch.manifest if fetch.is_ok else None

    remote_sources = remote_manifest.get("sources", {}) if remote_manifest else {}

    # Devices
    devices = _list_devices_warn(backend)

    console.print("\n[bold]Mind Meld Status[/bold]")
    console.print(f"  Device: {safe_str(device_name)} ({safe_str(device_id)})")

    # Surface the last autopull/autopush breadcrumb so a wedged sync
    # (silent lock contention, missing passphrase, bad config) is visible.
    crumbs = _read_autorun_breadcrumbs()
    discovery_nag = False
    for verb in ("pull", "push"):
        crumb = crumbs.get(verb)
        if not crumb:
            continue
        ts = crumb.get("timestamp", "?")
        outcome = crumb.get("outcome", "?")
        # safe_str the peer-reachable fields before they hit a Rich
        # console, which interprets markup and passes escapes through.
        # `detail` is fed raw `str(e)` by the `failed` / `config-error` /
        # `crypto-error` breadcrumb writers, and those exceptions can
        # carry peer-derived text (device names, source names, rel_paths
        # from a peer manifest). This is the render site that MATTERS:
        # `mm status` is the command the v0.12.16 degradation signal is
        # designed to reach, and it runs far more often than `mm diag`.
        detail = crumb.get("detail")
        outcome_str = (
            f"{safe_str(str(outcome))}: {safe_str(str(detail))}"
            if detail
            else safe_str(str(outcome))
        )
        # Staleness gate. `_write_autorun_breadcrumb` is called from INSIDE
        # the command, so a failure that happens before typer's runner --
        # an ImportError at module scope being the obvious one -- writes no
        # breadcrumb at all, and this line then reports the last SUCCESS
        # forever while sync is wedged. That is the one degradation the
        # v0.8.1 `no-sources` and v0.12.16 `degraded` breadcrumbs cannot
        # cover, because both are written by code that never ran.
        console.print(
            f"  Last auto-{safe_str(str(verb))}: {safe_str(str(ts))} ({outcome_str})"
            f"{_breadcrumb_staleness_suffix(ts)}"
        )
        if (
            verb == "push"
            and outcome == "degraded"
            and isinstance(detail, str)
            and "git repository discovery" in detail
        ):
            discovery_nag = True
    retro_nag = _print_retro_capture_status(sources_configs, device_id)
    if discovery_nag and not retro_nag:
        console.print(
            "  [yellow]Git repository discovery incomplete:[/yellow] run [bold]mm diag[/bold]"
        )
    if fetch.status == "missing":
        console.print("  [dim]Remote manifest: not yet pushed from this device.[/dim]")
    elif fetch.status == "corrupt":
        console.print(
            "  [yellow]Remote manifest: CORRUPT[/yellow] — next 'mm push' "
            "will attempt recovery from sidecar or peers."
        )

    # Visible-failure contract: surface the auto-command migration
    # breadcrumb so users notice their config is missing recommended
    # excludes (otherwise autopull/autopush silently keep producing
    # conflict copies for repo-mode.json / land-deploy-confirmed every
    # pull, with no signal that `mm migrate-config` would fix it).
    missing_excludes = _config_missing_recommended_excludes(config)
    if missing_excludes:
        console.print(
            f"  [yellow]Config missing recommended excludes for source(s):[/yellow] "
            f"{', '.join(safe_str(name) for name in missing_excludes)} — "
            "run [bold]mm migrate-config[/bold] to add."
        )

    try:
        n_conflicts = len(resolveflow._find_conflict_files(config))
    except OSError:
        n_conflicts = 0
    if n_conflicts:
        console.print(
            f"  [yellow]Unresolved conflicts: {n_conflicts}[/yellow] — "
            "run [bold]mm conflicts[/bold] to review, "
            "[bold]mm resolve[/bold] to pick a winner."
        )

    # Allowlist the BROKEN states, never a denylist of healthy ones. The full
    # argument lives beside the constant in `skill_link.BROKEN_SKILL_STATUSES`
    # -- do not restate the working-as-intended list here, it drifted once
    # already (this copy omitted `absent` and then `removed-by-user`).
    # Same shape as the Grok refusal that
    # pinned the breadcrumb at `degraded` and destroyed it as a signal. A
    # denylist also defaults every FUTURE status to "broken".
    skill_may_create = skill_link.consented_agent_keys(config, sources_configs)
    broken_skills = [
        row
        for row in skill_link.diagnose_skill_links(may_create=skill_may_create)
        if row.get("status") in skill_link.BROKEN_SKILL_STATUSES
    ]
    if broken_skills:
        rendered = _render_broken_skill_status(broken_skills[0])
        if "restart the agent so it reloads SKILL.md" not in rendered:
            rendered = f"{rendered}, then restart the agent so it reloads SKILL.md"
        console.print(f"  [yellow]Skill links broken:[/yellow] {rendered}")

    # Seam 3 — auto-upgrade nudge surfacing in status. Reads cache only,
    # no network call. Distinct from autopull/autopush emission (which gates
    # on last_nudged_at) — `mm status` is an explicit user check and shows
    # the cached result every time, regardless of the 24h re-emit gate.
    upgrade_result = upgrade.check_for_upgrade(config)
    if upgrade_result.state == "upgrade-available" and upgrade_result.latest:
        console.print(
            f"  [yellow]Upgrade available:[/yellow] "
            f"{safe_str(upgrade_result.local)} → {safe_str(upgrade_result.latest)} "
            f"(run [bold]{safe_str(upgrade_result.install_cmd)}[/bold])"
        )

    # Per-machine source-toggle visibility (v0.10.0). Two breadcrumbs:
    #   1. Disabled list: surfaces intentional state so future-you doesn't
    #      forget gstack is off and re-debug "why isn't this syncing".
    #   2. New-source hint: when DEFAULT_SOURCES grows (codex et al.) the
    #      user sees a one-shot suggestion to opt in. seen_sources tracks
    #      acknowledgments via enable/disable/reconfigure; the lazy-init
    #      invariant in seen_sources.read() ensures upgraders don't see
    #      spurious hints for already-shipped claude/gstack.
    disabled_list = list(config.get("sync", {}).get("disabled_sources", []) or [])
    if disabled_list:
        resolvable = {s["name"] for s in _resolve_all_configured_sources(config)}
        shown_disabled = [name for name in disabled_list if name in resolvable]
        if shown_disabled:
            console.print(
                f"  [yellow]Disabled sources (this device):[/yellow] "
                f"{', '.join(safe_str(name) for name in sorted(shown_disabled))} — "
                "run [bold]mm enable-source <name>[/bold] to re-enable."
            )

    explicit_names = [s["name"] for s in config.get("sync", {}).get("sources", []) or []]
    currently_resolved = [s["name"] for s in sources_configs]
    seen = seen_sources.read(initial=currently_resolved)
    default_names = [s["name"] for s in DEFAULT_SOURCES]
    new_sources = seen_sources.compute_new_sources(
        seen=seen,
        default_names=default_names,
        disabled=disabled_list,
        explicit_names=explicit_names,
    )
    for name in new_sources:
        console.print(
            f"  [cyan]New source available:[/cyan] {safe_str(name)} — "
            f"run [bold]mm enable-source {safe_str(name)}[/bold] to sync, "
            f"or [bold]mm disable-source {safe_str(name)}[/bold] to dismiss."
        )

    from mind_meld import host_usage as _host_usage

    # Codex has no consent bit to report (enabling the source is the consent),
    # so this line exists only for the state a user cannot otherwise see: a
    # cache mid-rebuild publishes less than it will, and autopush never warms.
    codex_diag = _host_usage.codex_usage_diag()
    if codex_diag.get("files_pre_track"):
        console.print(
            f"  Codex usage capture: rebuilding — {codex_diag['files_pre_track']} rollouts "
            "awaiting re-walk; run [bold]mm push[/bold] to finish it"
        )
    elif codex_diag.get("state") == "migrating":
        console.print(
            f"  Codex usage capture: warming — {codex_diag.get('pending') or 0} rollouts "
            "not yet scanned; run [bold]mm push[/bold] to finish it"
        )

    grok_source_on = any(s.get("name") == "grok" for s in sources_configs)
    grok_on = grok_source_on or grok_host_usage_enabled(config)
    grok_present = False
    try:
        grok_present = _host_usage.grok_sessions_root().exists()
    except OSError:
        grok_present = False
    if grok_on or grok_present:
        if grok_on:
            grok_diag = _host_usage.grok_usage_diag()
            last_reason = grok_diag.get("last_reason")
            if last_reason in events_tail._HOST_PERMANENT_REASONS:
                # Reuse the skip phrase so status, diag, and push stderr
                # cannot drift apart. A permanent reason is not fixed by
                # `mm push`.
                console.print(
                    "  Grok usage capture: enabled — "
                    + events_tail._host_skip_phrase("grok", str(last_reason))
                )
            elif grok_diag.get("complete_once") is True:
                console.print("  Grok usage capture: enabled; a prior scan completed successfully")
            else:
                console.print(
                    "  Grok usage capture: enabled, but no successful scan yet — "
                    "run [bold]mm push[/bold]"
                )
        else:
            console.print(
                "  Grok usage capture: disabled — "
                "run [bold]mm enable-source grok[/bold] to sync Grok customizations "
                "and publish token totals (session files stay local)."
            )

    # Per-source breakdown
    total_local = 0
    total_remote = 0
    total_new = 0
    total_modified = 0
    total_deleted = 0

    for src_name, src_data, remote_src, diff in iter_source_diffs(
        local_manifest, remote_sources, source_filter=source
    ):
        local_files = src_data["files"]
        remote_files = remote_src.get("files", {})

        local_count = len(local_files)
        remote_count = len(remote_files)
        total_local += local_count
        total_remote += remote_count
        total_new += len(diff.new)
        total_modified += len(diff.modified)
        total_deleted += len(diff.deleted)

        console.print(f"\n  [bold]Source '{safe_str(src_name)}':[/bold]")
        console.print(f"    Local files: {local_count}")
        console.print(f"    Remote files: {remote_count}")

        if diff.has_changes:
            console.print("    [yellow]Pending push:[/yellow]")
            if diff.new:
                console.print(f"      + {len(diff.new)} new")
            if diff.modified:
                console.print(f"      ~ {len(diff.modified)} modified")
            if diff.deleted:
                console.print(f"      - {len(diff.deleted)} deleted")
        else:
            console.print("    [green]In sync.[/green]")

    if not source:
        console.print(f"\n  Total local: {total_local}, Total remote: {total_remote}")

    if total_new or total_modified or total_deleted:
        console.print("\n  [yellow]Overall pending push:[/yellow]")
        console.print(
            f"    + {total_new} new, ~ {total_modified} modified, - {total_deleted} deleted"
        )
    elif _has_mtime_only_changes_vs_remote(local_manifest, remote_sources, source_filter=source):
        # Symmetric with _push_core's mtime-only republish gate (v0.12.6).
        # Without this branch, status would print "All sources in sync"
        # after `mm resolve (l)ocal` even though a push is needed to
        # propagate the bumped mtime to peers. source_filter mirrors the
        # iter_source_diffs filter above so `mm status --source X` doesn't
        # surface metadata-pending hints from sources Y, Z.
        console.print(
            "\n  [yellow]Metadata-only changes pending[/yellow] "
            "(run `mm push` to publish updated mtimes)."
        )
    elif not source:
        console.print("\n  [green]All sources in sync.[/green]")

    if len(devices) > 1:
        console.print("\n  Other devices:")
        for d in devices:
            if d["device_id"] != device_id:
                console.print(f"    {safe_str(d['device_name'])} ({safe_str(d['device_id'])})")


# ── diag ──────────────────────────────────────────────────────────────

_DISCOVERY_REJECT_REASONS = ("gone", "not-a-repo", "unreadable")


def _home_relative_path(path: Path) -> str:
    """Render ``path`` as ``~/...`` when it sits under $HOME."""
    try:
        resolved = path.expanduser()
        home = Path.home()
        try:
            return "~/" + resolved.relative_to(home).as_posix()
        except ValueError:
            return str(resolved)
    except OSError:
        return str(path)


_DIAG_MODELS_SHOWN = 6
"""How many model ids the plain-text ``mm diag`` names inline.

The producer already caps the list at ``host_usage._DIAG_MODEL_CAP`` (32);
this second, smaller bound is a readability one — the block is optimized for
paste into a support chat, and 32 ids on one line is not that. The COUNT is
always exact, so a truncated list never reads as the whole set.
"""


def _diag_models_line(state: dict) -> str:
    """One line of locally-cached model ids for a host-usage reader.

    Cache-only, like everything else in this block: these ids come from the
    reader's own private cache, never from a peer manifest. They still go
    through ``safe_str`` because a host log wrote them and Rich interprets
    markup in an f-string — the same reasoning as the breadcrumb ``detail``
    render above.

    ``safe_str`` alone is NOT enough here. It strips terminal escapes and Rich
    markup but leaves newlines, so a model id of ``"gpt-5\ngrok cache: ok"``
    forges an extra field into a block a user pastes into a support chat.
    Bucket to the same conservative whitelist ``aggregator._safe_short`` uses
    for the identical class of string; the two surfaces should not disagree
    about what a model id may contain.

    ``0`` is a real answer, not a gap: a cold or mid-rebuild cache has
    interned no ids yet, which is exactly what ``codex reader state`` two
    lines up already says.
    """
    count = state.get("model_count")
    count = count if isinstance(count, int) and not isinstance(count, bool) else 0
    raw = state.get("models")
    names = (
        [re.sub(r"[^A-Za-z0-9._\-() ]", "_", safe_str(str(m)))[:60] for m in raw]
        if isinstance(raw, list)
        else []
    )
    if not count or not names:
        return str(count)
    shown = names[:_DIAG_MODELS_SHOWN]
    hidden = count - len(shown)
    listed = ", ".join(shown) + (f", +{hidden} more" if hidden > 0 else "")
    return f"{count} ({listed})"


_DIAG_CAPTURE_CLAMP = 128
"""Bound on peer-controlled strings in the recorded-capture diag block.

The mm-push row arrives via the pull apply path and ``merge.merge_jsonl``,
so ``ts`` / ``mm_version`` / ``since`` / discovery labels are peer text.
The invariant requires bounding, not merely sanitizing; 128 matches
``aggregator._safe_short``."""


def _print_retro_capture_status(sources: list[dict], device_id: str) -> bool:
    """Nag on an incomplete recorded capture. Returns True if a nag printed."""
    mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if mm_events_src is None:
        return False
    events_dir = Path(mm_events_src["path"]).expanduser() / "events"
    projected = events.project_recorded_capture(events.latest_mm_push_row(events_dir, device_id))
    if projected is None:
        return False
    aborts = projected.get("walk_budget_aborts") or 0
    errs = projected.get("walk_errors") or 0
    skipped = aborts + errs
    incomplete = (not projected.get("advances_cursor", True)) or skipped > 0
    if not incomplete:
        return False
    since_s = projected.get("since") or projected.get("ts")
    since_day = since_s[:10] if isinstance(since_s, str) and len(since_s) >= 10 else "unknown"
    days = events.INITIAL_CURSOR_LOOKBACK_DAYS
    if isinstance(since_s, str):
        try:
            since_dt = datetime.fromisoformat(since_s)
            if since_dt.tzinfo is not None:
                delta_days = (datetime.now(timezone.utc) - since_dt).days
                days = max(1, min(events.RECAPTURE_WINDOW_MAX_DAYS, delta_days or 1))
        except (TypeError, ValueError):
            pass
    if skipped == 1:
        skip_phrase = "1 repository was skipped"
    elif skipped > 1:
        skip_phrase = f"{skipped} repositories were skipped"
    else:
        skip_phrase = "repository set incomplete"
    console.print(
        f"  [yellow]Retro capture:[/yellow] incomplete since {safe_str(since_day)} — {skip_phrase}."
    )
    console.print(f"  Recover this Mac's last {days} days: [bold]mm recapture {days}d[/bold]")
    console.print("  Details: [bold]mm diag[/bold]")
    return True


def _clamp_peer_text(value: object) -> str:
    return safe_str(str(value))[:_DIAG_CAPTURE_CLAMP]


def _sanitize_recorded_capture(projected: dict) -> dict:
    out = dict(projected)
    for key in ("ts", "mm_version", "discovery", "since"):
        if out.get(key) is not None:
            out[key] = _clamp_peer_text(out[key])
    return out


def _fresh_discovery_label(diag: dict) -> str:
    status = diag.get("status")
    if status in ("exceeded", "error"):
        return "partial"
    if status == "empty":
        return "empty"
    if status == "no-prober":
        return "not-run"
    if status == "complete":
        return "complete"
    return "not-run"


def _collect_git_capture_diag(config: dict, device_id: str | None, discovery: dict) -> dict:
    recorded = None
    if device_id:
        try:
            sources = get_sources(config) if config else []
            src = next((s for s in sources if s.get("name") == "mm-events"), None)
            if src is not None:
                events_dir = Path(src["path"]).expanduser() / "events"
                projected = events.project_recorded_capture(
                    events.latest_mm_push_row(events_dir, device_id)
                )
                if projected is not None:
                    recorded = _sanitize_recorded_capture(projected)
        except Exception:
            recorded = None
    return {
        "recorded": recorded,
        "fresh": {
            "discovery": _fresh_discovery_label(discovery),
            "status": discovery.get("status"),
            "roots": len(discovery.get("roots") or []),
            "exceeded": bool(discovery.get("exceeded")),
            "error_count": len(discovery.get("errors") or []),
        },
    }


def _collect_discovery_diag(config: dict) -> dict:
    """Run git-root discovery at the AUTOPUSH budget and shape it for diag.

    Autopush is the only path a Claude Code hook fires and the only one that
    loses data; reporting complete at the interactive 100 ms budget while
    autopush is exceeded at 50 ms would point the verification surface at
    the wrong number.
    """
    budget_ms = events.ROOT_DISCOVERY_BUDGET_AUTOPUSH_MS
    started = time.monotonic()
    try:
        result = events.discover_git_roots(
            config or {},
            deadline_monotonic=started + budget_ms / 1000.0,
        )
    except Exception as e:
        return {
            "budget_ms": budget_ms,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "status": "error",
            "error": f"{type(e).__name__}: {safe_str(e)}",
            "probers_ran": [],
            "roots": [],
            "attribution": [],
            "rejects": {"counts": {}, "sample": []},
            "errors": [],
            "exceeded": False,
        }
    elapsed_ms = round((time.monotonic() - started) * 1000, 3)
    if result.exceeded:
        status = "exceeded"
    elif not result.probers_ran:
        status = "no-prober"
    elif result.roots:
        status = "complete"
    else:
        status = "empty"
    counts = {reason: 0 for reason in _DISCOVERY_REJECT_REASONS}
    for reason, n in result.reject_counts:
        counts[reason] = n
    return {
        "budget_ms": budget_ms,
        "elapsed_ms": elapsed_ms,
        "status": status,
        "exceeded": result.exceeded,
        "probers_ran": list(result.probers_ran),
        "roots": [str(p) for p in result.roots],
        "attribution": [{"source": src, "path": str(p)} for src, p in result.attribution],
        "rejects": {
            "counts": counts,
            "sample": [{"reason": reason, "path": str(p)} for reason, p in result.rejects],
        },
        "errors": list(result.errors),
    }


def _collect_diag_state(backend: LocalBackend) -> dict:
    """Gather non-secret state for support triage.

    Secrets allowlist — NEVER include:
      * raw root_salt bytes (fingerprint only)
      * master_key (never computed here)
      * keycheck_blob contents
      * passphrase
      * peer device_ids (only counts)
      * grok inspect stdout / stderr / unknown fields / parse exceptions
      * host_skill_discovery values other than the four extracted fields
        (claude_skills_compat, retro_fleet_resolved, retro_fleet_path,
        grok_version) plus host/status
      * host_usage values other than the cache-only diag keys: grok's
        consented / complete_once / usage_less_skipped / last_reason /
        cache_state / model_count / models, and Codex's cache_state / state /
        files_cached / files_migrated / files_pre_track / files_on_disk /
        pending / model_count / models (never a path, never a host store,
        never a token magnitude)
      * local_emails (this machine's author-email trust set, and peers'
        after a pull merge) — project an allowlist, never render the row

    Uses existing tri-state helpers (`fetch_crypto_init`, `sidecar.read`)
    rather than re-sampling raw blob bytes — the tri-state branches are
    where corruption meaning lives, so bypassing them would misreport the
    exact scenario this command exists to diagnose.
    """
    # Local config (best-effort — a broken config is itself diag-worthy).
    skill_may_create: frozenset[str] | None = None
    skill_config_error: str | None = None
    resolved_sources: list[dict] = []
    cfg: dict = {}
    try:
        cfg = load_config()
        dev_id = cfg.get("device", {}).get("id")
        dev_name = cfg.get("device", {}).get("name")
        storage_path = cfg.get("storage", {}).get("path")
        local_fp = cfg.get("crypto", {}).get("root_salt_fp")
        config_state = "ok"
        resolved_sources = get_sources(cfg)
        skill_may_create = skill_link.consented_agent_keys(cfg, resolved_sources)
    except MindMeldError as e:
        dev_id = dev_name = storage_path = local_fp = None
        config_state = f"error: {e}"
        skill_config_error = str(e)
        skill_may_create = frozenset()

    # Storage crypto init (delegates tri-state to the source of truth).
    fetch = fetch_crypto_init(backend)
    if fetch.status == "ok":
        crypto_init = {
            "status": "ok",
            "root_salt_fp": root_salt_fingerprint(fetch.root_salt),
            "argon2_memory_kb": fetch.argon2_memory_kb,
        }
    else:
        crypto_init = {"status": fetch.status}

    # Sidecar (scoped by local device_id for device_id-mismatch detection).
    sidecar_info: dict = {"path": str(sidecar.sidecar_path())}
    try:
        sc = sidecar.read(dev_id) if dev_id else None
        if sc is None and sidecar.sidecar_path().exists():
            # sidecar exists on disk but read() returned None (device_id mismatch,
            # shape-invalid, etc.) — useful triage signal.
            sidecar_info["state"] = "present_but_rejected"
        elif sc is None:
            sidecar_info["state"] = "missing"
        else:
            sidecar_info["state"] = "ok"
            sidecar_info["device_id"] = sc.get("device_id")
            sidecar_info["timestamp"] = sc.get("timestamp")
    except Exception as e:  # very defensive — diag must not crash
        sidecar_info["state"] = f"error: {type(e).__name__}"

    # Storage inventory — counts only, no identifiers.
    def _count(prefix: str, suffix: str = ".enc") -> int:
        try:
            return sum(1 for k in backend.list_keys(prefix) if k.endswith(suffix))
        except Exception:
            return -1  # signals "could not enumerate"

    # For manifests/ and data/, count per-device sub-prefixes as "peer count."
    def _count_device_prefixes(prefix: str) -> int:
        try:
            prefixes = set()
            for k in backend.list_keys(prefix):
                parts = k.split("/")
                if len(parts) >= 3:
                    prefixes.add(parts[1])
            return len(prefixes)
        except Exception:
            return -1

    # Own manifest conflict copies via the existing find_conflict_copies
    # helper, if a local device_id is known.
    own_conflict_copies = -1
    if dev_id:
        try:
            # Predicate is validator-free because we don't need to decrypt;
            # count all sibling files that match the conflict-name pattern.
            own_conflict_copies = len(
                backend.find_conflict_copies(manifest_key(dev_id), lambda p: True)
            )
        except Exception:
            pass

    storage_inv = {
        "manifests_total": _count(MANIFESTS_PREFIX),
        "manifest_peer_count": _count_device_prefixes(MANIFESTS_PREFIX),
        "data_peer_count": _count_device_prefixes(DATA_PREFIX),
        "devices_total": _count(DEVICES_PREFIX, suffix=".json"),
        "own_manifest_conflict_copies": own_conflict_copies,
    }

    # Last autorun breadcrumb (ops-oriented: when did autopush/autopull
    # last fire and how did it end). Keyed per verb since v0.12.45 so
    # autopull cannot erase a degraded autopush crumb.
    breadcrumb: dict | None = None
    try:
        bp = _autorun_breadcrumb_path()
        if bp.exists():
            breadcrumb = _read_autorun_breadcrumbs()
            if not breadcrumb:
                breadcrumb = {"error": "unreadable"}
    except (OSError, ValueError):
        breadcrumb = {"error": "unreadable"}

    discovery = _collect_discovery_diag(cfg)
    from mind_meld import host_usage as _host_usage

    grok_consented: bool | None = None
    if config_state == "ok":
        # Source-enabled is consent (Track 22B), using the exact resolved
        # source set already needed for skill policy above. The 21A bit remains
        # an OR. This is cache-only with respect to host usage: get_sources()
        # resolves configuration but never opens the Grok host store.
        grok_consented = grok_host_usage_enabled(cfg) or any(
            source.get("name") == "grok" for source in resolved_sources
        )
    grok_diag = _host_usage.grok_usage_diag()
    host_usage_state = {
        "grok": {
            "consented": grok_consented,
            "complete_once": grok_diag["complete_once"],
            "usage_less_skipped": grok_diag["usage_less_skipped"],
            "last_reason": grok_diag.get("last_reason"),
            "cache_state": grok_diag["cache_state"],
            "model_count": grok_diag.get("model_count", 0),
            "models": grok_diag.get("models", []),
        },
        # Codex needs its own block for the same reason Grok does: a reader
        # whose cache is mid-rebuild publishes less than it will, and nothing
        # else on any surface says so. Cache-only, so this stays inside diag's
        # no-passphrase contract.
        "codex": _host_usage.codex_usage_diag(),
    }
    return {
        "mm_version": __version__,
        "config": {
            "state": config_state,
            "device_id": dev_id,
            "device_name": dev_name,
            "storage_path": storage_path,
            "root_salt_fp": local_fp,  # fingerprint only, never the raw salt
        },
        "crypto_init": crypto_init,
        "root_salt_drift": (
            "ok"
            if (local_fp and crypto_init.get("root_salt_fp") == local_fp)
            else "mismatch"
            if (local_fp and crypto_init.get("status") == "ok")
            else "n/a"
        ),
        "sidecar": sidecar_info,
        "storage_inventory": storage_inv,
        "last_autorun": breadcrumb,
        "skill_links": skill_link.diagnose_skill_links(
            may_create=skill_may_create, config_error=skill_config_error
        ),
        "host_skill_discovery": host_skill_discovery.probe_grok_skill_discovery(),
        "host_usage": host_usage_state,
        "discovery": discovery,
        "git_capture": _collect_git_capture_diag(cfg, dev_id, discovery),
    }


@app.command()
def diag(
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of plain text."
    ),
) -> None:
    """Dump non-secret crypto + sync state for support triage.

    Never prints raw root_salt, master_key, keycheck, passphrase, or peer
    device_ids. See SPEC.md "Manifest corruption recovery" for how this
    maps to the tri-state recovery chain — each field here names one
    possible cause of a recovery failure.
    """
    # Resolve storage path WITHOUT a valid config: diag has to run even
    # when config is broken (that's literally one of the things users run
    # it to debug). Fall back to the default storage path.
    try:
        cfg = load_config()
        storage_path = cfg["storage"]["path"]
    except MindMeldError:
        storage_path = DEFAULT_STORAGE_PATH
    full_path = Path(storage_path).expanduser()
    backend = LocalBackend(str(full_path))

    state = _collect_diag_state(backend)

    if as_json:
        import json as _json

        # Emit via typer.echo, NOT console.print: Rich would reflow the JSON
        # (word-wrap, style markup) and break downstream consumers.
        typer.echo(_json.dumps(state, indent=2, sort_keys=True, default=str))
        return

    # Plain text formatting — optimized for support-chat paste.
    console.print(f"[bold]Mind Meld diag (v{state['mm_version']})[/bold]\n")

    cfg_state = state["config"]
    console.print("[bold]Config[/bold]")
    console.print(f"  state:         {cfg_state['state']}")
    console.print(f"  device_id:     {cfg_state['device_id'] or '(none)'}")
    console.print(f"  device_name:   {cfg_state['device_name'] or '(none)'}")
    console.print(f"  storage_path:  {cfg_state['storage_path'] or '(default)'}")
    console.print(f"  root_salt_fp:  {cfg_state['root_salt_fp'] or '(unset)'}")

    ci = state["crypto_init"]
    console.print("\n[bold]mm-crypto-init[/bold]")
    console.print(f"  status:        {ci.get('status')}")
    if ci.get("status") == "ok":
        console.print(f"  root_salt_fp:  {ci.get('root_salt_fp')}")
        console.print(f"  argon2 mem kb: {ci.get('argon2_memory_kb')}")
    console.print(f"  drift check:   {state['root_salt_drift']}")

    sc = state["sidecar"]
    console.print("\n[bold]Sidecar (local last-push snapshot)[/bold]")
    console.print(f"  path:          {sc.get('path')}")
    console.print(f"  state:         {sc.get('state')}")
    if sc.get("device_id"):
        console.print(f"  device_id:     {sc.get('device_id')}")
    if sc.get("timestamp"):
        console.print(f"  timestamp:     {sc.get('timestamp')}")

    inv = state["storage_inventory"]
    console.print("\n[bold]Storage inventory[/bold]")
    console.print(f"  manifests total:         {inv['manifests_total']}")
    console.print(f"  manifest peer count:     {inv['manifest_peer_count']}")
    console.print(f"  data peer count:         {inv['data_peer_count']}")
    console.print(f"  devices/ entries:        {inv['devices_total']}")
    console.print(f"  own manifest conflicts:  {inv['own_manifest_conflict_copies']}")

    console.print("\n[bold]Last autorun[/bold]")
    br = state["last_autorun"]
    if br is None:
        console.print("  (no autopull/autopush has run on this device yet)")
    elif "error" in br:
        console.print("  [yellow]breadcrumb unreadable[/yellow]")
    else:
        rendered_any = False
        for verb in ("pull", "push"):
            entry = br.get(verb)
            if not isinstance(entry, dict):
                continue
            rendered_any = True
            console.print(f"  {verb}:")
            console.print(f"    outcome:    {safe_str(str(entry.get('outcome', '')))}")
            console.print(f"    timestamp:  {safe_str(str(entry.get('timestamp', '')))}")
            if entry.get("detail"):
                # safe_str at the render site covers every producer at once:
                # sibling `_write_autorun_breadcrumb(verb, "failed"/"config-error",
                # str(e))` calls can carry peer-derived text (device names, source
                # names, rel_paths from a peer manifest) into this field, and Rich
                # interprets markup in an f-string. The v0.12.16 degradation
                # strings are all literals, but the field is shared.
                console.print(f"    detail:     {safe_str(str(entry.get('detail')))}")
        if not rendered_any:
            console.print("  [yellow]breadcrumb unreadable[/yellow]")

    console.print("\n[bold]Skill links[/bold]")
    for row in state.get("skill_links") or []:
        agent = safe_str(str(row.get("agent", "")))
        status = safe_str(str(row.get("status", "")))
        target = safe_str(str(row.get("target", "")))
        detail = row.get("readlink") or row.get("detail") or row.get("store_state")
        extra = f" ({safe_str(str(detail))})" if detail else ""
        policy = row.get("maintain_links") or ""
        console.print(f"  {agent}: {status}{extra}")
        if policy:
            console.print(f"    maintain_links: {safe_str(str(policy))}")
        if status not in ("ok", "absent") and target:
            console.print(f"    {target}")

    hsd = state.get("host_skill_discovery") or {}
    console.print("\n[bold]Host skill discovery[/bold]")
    console.print(f"  host:                  {safe_str(str(hsd.get('host', '')))[:200]}")
    console.print(f"  status:                {safe_str(str(hsd.get('status', '')))[:200]}")
    if hsd.get("status") == host_skill_discovery.STATUS_OK:
        console.print(f"  claude_skills_compat:  {hsd.get('claude_skills_compat')}")
        console.print(f"  retro_fleet_resolved:  {hsd.get('retro_fleet_resolved')}")
        path = hsd.get("retro_fleet_path")
        console.print(f"  retro_fleet_path:      {safe_str(str(path))[:200] if path else '(none)'}")
        console.print(
            f"  grok_version:          {safe_str(str(hsd.get('grok_version') or ''))[:200]}"
        )

    hu_state = (state.get("host_usage") or {}).get("grok") or {}
    console.print("\n[bold]Host usage[/bold]")
    consented = hu_state.get("consented")
    if consented is None:
        consented_shown = "(config unreadable)"
    else:
        consented_shown = "yes" if consented else "no"
    if hu_state.get("complete_once"):
        scan_shown = "yes"
    elif hu_state.get("cache_state") == "unreadable":
        scan_shown = "unreadable"
    else:
        scan_shown = "no"
    console.print(f"  grok consented:          {consented_shown}")
    console.print(f"  grok prior successful scan: {scan_shown}")
    last_reason = hu_state.get("last_reason")
    if last_reason in events_tail._HOST_PERMANENT_REASONS:
        console.print(
            "  grok last failure:        " + events_tail._host_skip_phrase("grok", str(last_reason))
        )
    console.print(f"  grok usage-less skipped: {hu_state.get('usage_less_skipped', 0)}")
    console.print(f"  grok cache:              {safe_str(str(hu_state.get('cache_state', '')))}")
    console.print(f"  grok models cached:      {_diag_models_line(hu_state)}")

    cx_state = (state.get("host_usage") or {}).get("codex") or {}
    console.print(f"  codex cache:             {safe_str(str(cx_state.get('cache_state', '')))}")
    console.print(f"  codex reader state:      {safe_str(str(cx_state.get('state', '')))}")
    cx_disk = cx_state.get("files_on_disk")
    console.print(
        f"  codex rollouts cached:   {cx_state.get('files_cached', 0)}"
        f" of {'unknown' if cx_disk is None else cx_disk}"
    )
    console.print(f"  codex models cached:     {_diag_models_line(cx_state)}")
    if cx_state.get("files_pre_track"):
        # The actionable half: these entries predate per-turn accounting and
        # are re-walked once. Autopush never warms the host cache, so on a
        # quiet Mac the nudge is the only way a user learns to finish it.
        console.print(
            f"  codex awaiting re-walk:  {cx_state['files_pre_track']}"
            " — run [bold]mm push[/bold] (interactive) to finish the rebuild"
        )

    disc = state.get("discovery") or {}
    console.print("\n[bold]Git-root discovery[/bold] (autopush budget)")
    console.print(f"  budget_ms:    {disc.get('budget_ms')}")
    console.print(f"  elapsed_ms:   {disc.get('elapsed_ms')}")
    console.print(f"  status:       {safe_str(str(disc.get('status', '')))}")
    if disc.get("status") == "error":
        console.print(f"  error:        {safe_str(str(disc.get('error', '')))}")
    probers = disc.get("probers_ran") or []
    if not probers:
        console.print("  probers:      (none ran — claude source disabled)")
    else:
        console.print(f"  probers:      {safe_str(', '.join(str(p) for p in probers))}")
    roots = disc.get("roots") or []
    console.print(f"  roots:        {len(roots)}")
    for raw in roots:
        shown = _home_relative_path(Path(str(raw)))
        console.print(f"    {safe_str(shown)}")
    counts = (disc.get("rejects") or {}).get("counts") or {}
    parts = [
        f"{counts[reason]} {reason}" for reason in _DISCOVERY_REJECT_REASONS if counts.get(reason)
    ]
    console.print(f"  rejects:      {', '.join(parts) if parts else '0'}")
    if disc.get("errors"):
        console.print(f"  errors:       {len(disc['errors'])}")
        for err in disc["errors"]:
            console.print(f"    {safe_str(str(err))}")

    cap = state.get("git_capture") or {}
    console.print("\n[bold]Git capture[/bold]")
    recorded = cap.get("recorded")
    if not recorded:
        console.print("  recorded:     (none — no mm-push row yet)")
    else:
        console.print("  recorded:")
        console.print(f"    ts:                  {recorded.get('ts') or '(none)'}")
        console.print(f"    mm_version:          {recorded.get('mm_version') or '(none)'}")
        console.print(
            f"    discovery:           {recorded.get('discovery') or '(legacy — no key)'}"
        )
        console.print(f"    since:               {recorded.get('since') or '(none)'}")
        console.print(f"    walk_budget_aborts:  {recorded.get('walk_budget_aborts')}")
        console.print(f"    walk_errors:         {recorded.get('walk_errors')}")
        console.print(f"    advances_cursor:     {recorded.get('advances_cursor')}")
    fresh = cap.get("fresh") or {}
    console.print("  fresh:")
    console.print(f"    discovery:           {fresh.get('discovery')}")
    console.print(f"    status:              {safe_str(str(fresh.get('status', '')))}")
    console.print(f"    roots:               {fresh.get('roots')}")
    console.print(f"    exceeded:            {fresh.get('exceeded')}")
    console.print(f"    error_count:         {fresh.get('error_count')}")


# ── devices ───────────────────────────────────────────────────────────


@app.command()
def devices(
    fmt: str = typer.Option(
        "table",
        "--format",
        help="Output format: 'table' (human) or 'json' (machine).",
        case_sensitive=False,
    ),
) -> None:
    """List all registered devices.

    ``--format=json`` emits a single JSON array on stdout. Each entry has the
    keys ``device_id``, ``device_name``, ``last_seen``, ``last_seen_version``;
    missing fields are emitted as ``null`` (not em-dashes \u2014 em-dashes are a
    table-rendering convention). Empty fleet renders as ``[]``. Stable contract
    for the Group 8 retro-fleet skill's ``mm devices --format=json`` consumer.
    """
    config = _get_config()
    backend = get_backend(config)
    device_list = _list_devices_warn(backend)
    my_id = config["device"]["id"]

    fmt_lower = fmt.lower()
    if fmt_lower not in ("table", "json"):
        _error(f"--format must be 'table' or 'json', got {fmt!r}")
        return  # unreachable

    if fmt_lower == "json":
        # Stable contract for the retro-fleet skill: emit a JSON list with
        # canonical keys. Render missing fields as null (not em-dashes \u2014
        # em-dashes are a table-side rendering convention). Print to stdout
        # so subprocess consumers can capture it cleanly.
        #
        # Sort by device_id for stable output across platforms \u2014 list_devices
        # iterates the storage directory and macOS APFS typically yields
        # alphabetical order, but Linux ext4 and other filesystems do not.
        # Cross-platform peers must see the same order or any consumer's
        # diff against a prior snapshot would flap.
        sorted_devices = sorted(device_list, key=lambda d: d["device_id"])
        records = [
            {
                "device_id": d["device_id"],
                "device_name": d.get("device_name"),
                "last_seen": d.get("last_seen"),
                "last_seen_version": d.get("last_seen_version"),
                "is_self": d["device_id"] == my_id,
            }
            for d in sorted_devices
        ]
        # Use plain print, not console \u2014 Rich injects styling that breaks the
        # JSON contract. sort_keys=True for stable per-record key order.
        print(json.dumps(records, sort_keys=True))
        return

    if not device_list:
        console.print("[yellow]No devices registered.[/yellow]")
        return

    table = Table(title="Registered Devices")
    table.add_column("Name")
    table.add_column("ID")
    table.add_column("Last Push")
    table.add_column("Version")
    table.add_column("")

    for d in device_list:
        marker = "[green]\u2190 this device[/green]" if d["device_id"] == my_id else ""
        # `last_seen` is seeded only on push (not at register time), so a
        # registered-but-never-pushed device renders as an em-dash rather
        # than misleadingly showing its registration time.
        # `last_seen_version` (v0.9.2+) records the mm version on the
        # peer's last push. Missing value => peer hasn't pushed since
        # upgrading to v0.9.2 \u2014 surface as em-dash so users can spot
        # pre-v0.9.2 peers that are blocking pull via the fleet-version
        # refusal gate.
        # device_name, device_id, last_seen_version are peer-controlled
        # JSON values. Rich Table cells interpret markup AND pass raw
        # terminal escapes through (verified). Sanitize before render.
        # Group 7 preflight #1 sweep extension (adversarial #3).
        table.add_row(
            safe_str(d.get("device_name", "?")),
            safe_str(d["device_id"]),
            safe_str(d.get("last_seen", "\u2014")),
            safe_str(d.get("last_seen_version", "\u2014")),
            marker,
        )

    console.print(table)


# ── diff ──────────────────────────────────────────────────────────────


@app.command(name="diff")
def diff_cmd(
    from_device: str | None = typer.Option(None, "--from", help="Diff against a specific device"),
    source: str | None = typer.Option(None, "--source", help="Diff a specific source only"),
) -> None:
    """Show what would change without applying (dry run)."""
    config = _get_config()
    passphrase = _get_passphrase_or_exit()
    device_id = config["device"]["id"]
    device_name = config["device"]["name"]
    max_file_size = config["sync"]["max_file_size"]

    backend = get_backend(config)
    try:
        memory_kb = _init_crypto_session(backend, passphrase, config)
    except MindMeldError as e:
        _error(str(e))

    target_id = from_device or device_id

    # Build local manifest (v2)
    sources_configs = get_sources(config)
    local_manifest = build_manifest_v2(device_id, device_name, sources_configs, max_file_size)

    diff_fetch = _fetch_remote_manifest(backend, target_id, passphrase, memory_kb)
    if diff_fetch.status == "missing":
        console.print(
            f"[dim]No remote manifest for "
            f"{'device ' + target_id if from_device else 'this device'} yet.[/dim]"
        )
    elif diff_fetch.status == "corrupt":
        console.print(
            f"[yellow]Warning:[/yellow] remote manifest for "
            f"{'device ' + target_id if from_device else 'this device'} is "
            f"corrupt — showing diff against empty remote."
        )
    # diff_fetch.manifest is pre-normalized via load_manifest.
    remote_manifest = diff_fetch.manifest if diff_fetch.is_ok else None

    remote_sources = remote_manifest.get("sources", {}) if remote_manifest else {}

    console.print(
        f"\n[bold]Diff against {'device ' + target_id if from_device else 'remote'}:[/bold]"
    )

    # Map source names → local base paths for pull-outcome prediction
    src_base_paths: dict[str, Path] = {
        s["name"]: Path(s["path"]).expanduser().resolve() for s in sources_configs
    }

    any_changes = False
    for src_name, src_data, remote_src, diff in iter_source_diffs(
        local_manifest, remote_sources, source_filter=source, skip_unchanged=True
    ):
        remote_files = remote_src.get("files", {})

        any_changes = True
        console.print(f"\n  [bold]Source '{safe_str(src_name)}':[/bold]")
        console.print("  [dim](push direction: local → remote)[/dim]")
        for path in sorted(diff.new):
            console.print(f"    [green]+ push  [/green] {safe_str(path)}")
        for path in sorted(diff.modified):
            # Predict what pulling the REMOTE version would do to local. The
            # manifests were built from the same state, so local hash matches
            # src_data; remote hash is the divergent one.
            remote_info = remote_files.get(path, {})
            base_path = src_base_paths.get(src_name)
            if base_path is not None and remote_info:
                outcome = _predict_pull_outcome(path, remote_info, base_path)
                console.print(
                    f"    [yellow]~ push  [/yellow] {safe_str(path)} (pull would: {outcome})"
                )
            else:
                console.print(f"    [yellow]~ push  [/yellow] {safe_str(path)}")
        for path in sorted(diff.deleted):
            console.print(f"    [red]- only-remote[/red] {safe_str(path)}")

    if not any_changes:
        console.print("[green]No differences.[/green]")


# ── gc ────────────────────────────────────────────────────────────────


@app.command()
def gc(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview orphan blobs and retention cleanup without deleting",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    prune_conflicts: bool = typer.Option(
        False,
        "--conflicts",
        help=f"Also delete .sync-conflict-* files older than {CONFLICT_AGE_DAYS} days",
    ),
) -> None:
    """Garbage collect orphaned blobs and local retention data.

    ``--dry-run`` reports blob and retention candidates without changing files,
    including a preview of the conflict-sidecar reaper. ``--conflicts`` is
    required to actually reap stale conflict copies.
    """
    config = _get_config()
    passphrase = _get_passphrase_or_exit()

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    try:
        backend = get_backend(config)
        try:
            memory_kb = _init_crypto_session(backend, passphrase, config)
        except MindMeldError as e:
            _error(str(e))
        _do_gc(config, passphrase, memory_kb, dry_run, verbose)
        # Track 7B: events retention is always-on (fleet policy, not opt-in).
        # See `_gc_old_event_files` for the tombstone-propagation framing.
        retention._gc_old_event_files(config, dry_run, verbose)
        # v0.11.14+: token cache reaper. Stale entries (no living jsonl OR
        # by_day older than 90d) are dropped. Dry-run reports without
        # mutating the cache file.
        retention._gc_token_cache(dry_run, verbose)
        # v0.12.39: leftover v0.12.0 trend-snapshot files. Best-effort,
        # filename-matched only, then rmdir if empty. Never rm -rf.
        retention._gc_orphan_retros_dir(dry_run, verbose)
        # Bare `--dry-run` previews the conflict reaper (the one that
        # touches user content). Apply still requires `--conflicts`.
        if prune_conflicts or dry_run:
            retention._gc_old_conflict_files(config, dry_run, verbose)
    finally:
        release_lock()


def _do_gc(
    config: dict,
    passphrase: str,
    memory_kb: int,
    dry_run: bool,
    verbose: bool,
    *,
    emit_retention_summary: bool = True,
) -> int:
    """Run garbage collection. Returns number of orphaned blobs found/deleted."""
    backend = get_backend(config)
    my_device_id = config["device"]["id"]

    # Sweep this device's stale tmp*.tmp files before ref-counting.
    # Runs UNDER the caller's lock (acquire_lock already held by gc()),
    # so no concurrent writer can race with the sweep.
    retention._sweep_local_tmp_files(
        backend,
        my_device_id,
        dry_run,
        verbose,
        emit_summary=emit_retention_summary,
    )

    # Collect all referenced hashes from ALL device manifests
    devices = _list_devices_warn(backend)
    referenced_hashes: set[str] = set()

    corrupt_devices: list[str] = []
    for device in devices:
        did = device["device_id"]
        gc_fetch = _fetch_remote_manifest(backend, did, passphrase, memory_kb)
        if gc_fetch.status == "missing":
            continue
        if gc_fetch.status == "corrupt":
            # A corrupt manifest may reference blobs we'd otherwise orphan.
            # Collect the IDs so we can refuse in write mode.
            corrupt_devices.append(f"{device.get('device_name', did)} ({did})")
            continue

        # gc_fetch.manifest is pre-normalized via load_manifest.
        manifest = gc_fetch.manifest

        # Iterate sources.*.files to collect hashes
        for src_data in manifest.get("sources", {}).values():
            for info in src_data.get("files", {}).values():
                referenced_hashes.add(info["sha256"])

    if corrupt_devices:
        msg = (
            "cannot GC safely — manifest(s) corrupt on: "
            f"{', '.join(corrupt_devices)}. Those manifests may reference "
            "blobs that would be reaped as orphans. Run 'mm push' on each "
            "affected device to recover the manifest, then retry GC."
        )
        # Refuse in BOTH modes. Printing an orphan list in dry-run with
        # corrupt manifests would mislead: the list is incomplete because
        # the corrupt manifest's referenced hashes are missing from
        # referenced_hashes. A user who copies that list into a separate
        # delete flow would reap live data.
        _error(msg)

    # List all blobs across all devices
    all_blobs = backend.list_keys(DATA_PREFIX)
    orphan_count = 0
    malformed_count = 0

    for bkey in all_blobs:
        if not bkey.endswith(".enc"):
            continue
        parsed = parse_blob_key(bkey)
        if parsed is None:
            # Wrong-depth .enc under data/ — not a known blob shape. Could be
            # a misplaced artifact from a future format, an external write, or
            # corruption. Surface it (verbose/dry-run) so the user can audit;
            # never auto-reap (we don't know what it is). `.tmp` artifacts from
            # crashed pushes are handled separately by _sweep_local_tmp_files
            # at the start of _do_gc, so they're not seen here.
            malformed_count += 1
            if verbose or dry_run:
                console.print(f"  [yellow]malformed (skipped):[/yellow] {bkey}")
            continue
        _did, sha = parsed
        if sha not in referenced_hashes:
            orphan_count += 1
            if verbose:
                console.print(f"  [red]orphan:[/red] {bkey}")
            if not dry_run:
                backend.delete(bkey)

    if malformed_count and not (verbose or dry_run):
        # Always summarize malformed-path count even in non-verbose mode, so
        # the user has a signal that something weird is sitting in storage.
        console.print(
            f"  [yellow]warning:[/yellow] {malformed_count} blob(s) at unexpected "
            f"path depth (skipped; run with --verbose to see)."
        )

    if dry_run:
        console.print(f"\n[bold]Dry run:[/bold] {orphan_count} orphaned blobs found.")
    else:
        console.print(
            f"\n[bold green]GC complete.[/bold green] Deleted {orphan_count} orphaned blobs."
        )

    return orphan_count


# ── sources ───────────────────────────────────────────────────────────


def _resolve_all_configured_sources(config: dict) -> list[dict[str, Any]]:
    """Return the resolution-time view of configured sources, BYPASSING the
    [sync].disabled_sources filter (path-existence filter still applies).

    Used by `mm sources` so the table can show disabled rows alongside
    active ones with an Enabled column. Mutating-via-copy preserves
    `get_sources()` as the only authoritative resolver.
    """
    sync = dict(config.get("sync", {}) or {})
    sync["disabled_sources"] = []
    config_no_disable = {**config, "sync": sync}
    return get_sources(config_no_disable)


@app.command()
def sources() -> None:
    """List configured sync sources.

    Shows ALL configured sources (active + disabled). The "Enabled" column
    reflects [sync].disabled_sources — disable a source on this device with
    `mm disable-source <name>`. The "Excluded" column reports how many files
    the source's `exclude_patterns` actually matched on this scan; diagnostic
    only, used to sanity-check an over-broad glob.
    """
    config = _get_config()

    src_list = _resolve_all_configured_sources(config)
    disabled_set = set(config.get("sync", {}).get("disabled_sources", []) or [])
    max_file_size = config["sync"]["max_file_size"]

    table = Table(title="Configured Sources")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Type")
    table.add_column("Enabled")
    table.add_column("Files")
    table.add_column("Excluded")

    for src in src_list:
        # Walk WITHOUT exclude_patterns so we count what the per-source
        # globs would have stripped. `all_files` is already post-EXCLUDED
        # (the global junk list runs inside `_record_file`), so any match
        # against `patterns` here is attributable to per-source globs only.
        # Two passes is the cheapest accurate diagnostic — instrumenting
        # the walker with a counter would leak diagnostic state into the
        # hot path.
        src_no_excludes = {k: v for k, v in src.items() if k != "exclude_patterns"}
        _, all_files = walk_source(src_no_excludes, max_file_size)
        patterns = src.get("exclude_patterns") or []
        if patterns:
            excluded_count = sum(1 for rel in all_files if _manifest_is_excluded(rel, patterns))
            kept = len(all_files) - excluded_count
            excluded_display = str(excluded_count)
        else:
            kept = len(all_files)
            excluded_display = "—"
        is_disabled = src["name"] in disabled_set
        enabled_display = "[red]N[/red]" if is_disabled else "[green]Y[/green]"
        # Dim the row's data columns when disabled to make the state
        # obvious at a glance.
        wrap = (lambda s: f"[dim]{s}[/dim]") if is_disabled else (lambda s: s)
        table.add_row(
            wrap(src["name"]),
            wrap(src["path"]),
            wrap(src["type"]),
            enabled_display,
            wrap(str(kept)),
            wrap(excluded_display),
        )

    console.print(table)


# ── enable-source / disable-source / reconfigure-sources ──────────────


def _known_source_names(config: dict) -> list[str]:
    """Sorted union of explicit-config names and DEFAULT_SOURCES names.

    The validation surface for `mm enable-source` / `mm disable-source`.
    A name is "known" if either the user has it in [[sync.sources]] or
    mm ships a default for it. Strict by default; --force accepts unknown.
    """
    explicit = [s["name"] for s in config.get("sync", {}).get("sources", []) or []]
    defaults = [s["name"] for s in DEFAULT_SOURCES]
    return sorted(set(explicit) | set(defaults))


def _validate_source_name(name: str, config: dict, *, force: bool) -> None:
    """Raise ConfigError on unknown name unless --force.

    Closest-match hint via difflib so a typo like `gstck` suggests `gstack`.
    --force surfaces a stderr breadcrumb but accepts (forward-compat:
    pre-disabling a not-yet-shipped source like codex).
    """
    valid = _known_source_names(config)
    if name in valid:
        return
    if force:
        print(
            f"mm: warning: '{name}' is not a known source "
            f"(valid: {', '.join(valid)}); accepting via --force.",
            file=sys.stderr,
        )
        return
    matches = difflib.get_close_matches(name, valid, n=1, cutoff=0.6)
    suggestion = f" Did you mean '{matches[0]}'?" if matches else ""
    if name == "opencode":
        raise ConfigError(
            f"unknown source '{name}' — valid: {', '.join(valid)}.{suggestion} "
            "The opencode sync source was retired in v0.12.55; see the README "
            "troubleshooting entry."
        )
    raise ConfigError(
        f"unknown source '{name}' — valid: {', '.join(valid)}.{suggestion} "
        "Use --force to accept a name not yet known to mm "
        "(forward-compat for not-yet-shipped sources)."
    )


def _set_grok_host_usage(config: dict, *, enabled: bool, quiet: bool = False) -> None:
    """Persist Grok usage consent. Does not touch sync.sources."""
    current = grok_host_usage_enabled(config)
    if current is enabled:
        if not quiet:
            state = "enabled" if enabled else "disabled"
            console.print(f"[dim]Grok usage capture is already {state}.[/dim]")
        return
    patch_config_on_disk({"retro": {"grok_host_usage": enabled}})
    if quiet:
        return
    if enabled:
        console.print("[green]Enabled Grok usage capture on this device.[/green]")
        console.print(
            "[dim]Reads terminal token totals from local Grok session updates. "
            "Session files are not synced. Prompts never leave the Mac.[/dim]"
        )
        console.print("[dim]Run 'mm disable-source grok' to turn this off.[/dim]")
    else:
        console.print("[green]Disabled Grok usage capture on this device.[/green]")
        console.print("[dim]Run 'mm enable-source grok' to turn this back on.[/dim]")


def _record_seen(names: list[str]) -> None:
    """Mark `names` as acknowledged in the seen_sources tracker.

    Atomic under flock via `seen_sources.acknowledge` (codex 2026-04-25:
    the previous read+update+write split lost concurrent acknowledgments).
    Best-effort: a write failure surfaces a stderr breadcrumb but does not
    crash the calling command. The seen tracker drives the `mm status`
    new-source hint; losing it just means a hint repeats.
    """
    config = _get_config()
    currently_resolved = [s["name"] for s in get_sources(config)]
    seen_sources.acknowledge(names, initial=currently_resolved)


@app.command(name="disable-source")
def disable_source(
    name: str = typer.Argument(..., help="Source name to disable on this device"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Accept a source name not yet known to mm (forward-compat).",
    ),
) -> None:
    """Disable a sync source on THIS DEVICE only.

    config.toml is per-machine (never synced), so `disabled_sources` is a
    natural per-device preference. Disabling does NOT delete the source's
    [[sync.sources]] entry — re-enabling preserves user customizations
    (include_dirs, exclude_patterns).

    Strict by default: unknown name errors with a closest-match hint.
    `--force` accepts unknown names so you can pre-disable a source that
    hasn't shipped yet (e.g. `mm disable-source codex --force`).
    """
    config = _get_config()
    try:
        _validate_source_name(name, config, force=force)
    except ConfigError as e:
        _error(str(e))

    sync = dict(config.get("sync", {}) or {})
    disabled = list(sync.get("disabled_sources", []) or [])
    if name in disabled:
        if name == "grok":
            _set_grok_host_usage(config, enabled=False, quiet=True)
        console.print(f"[dim]Source '{name}' is already disabled.[/dim]")
        return

    disabled.append(name)
    updates: dict[str, dict[str, Any]] = {"sync": {"disabled_sources": sorted(disabled)}}
    if name == "grok":
        updates["retro"] = {"grok_host_usage": False}
    patch_config_on_disk(updates)
    _record_seen([name])

    console.print(f"[green]Disabled source '{name}' on this device.[/green]")
    console.print(f"[dim]Run 'mm enable-source {name}' to re-enable.[/dim]")


@app.command(name="enable-source")
def enable_source(
    name: str = typer.Argument(..., help="Source name to enable on this device"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Accept a source name not yet known to mm (forward-compat).",
    ),
) -> None:
    """Enable a sync source on this device.

    Removes the name from [sync].disabled_sources. If the name is in
    DEFAULT_SOURCES but absent from the user's [[sync.sources]] (e.g.
    a freshly-shipped codex source that didn't auto-enable), append the
    default config so the source actually starts syncing.

    Enabling `codex` or `grok` ALSO authorizes that host's local
    usage reader, whose activity shows up in the `retro-fleet` AGENT LOGS
    block, and (for `codex` / `claude`) authorizes mm to
    maintain a `retro-fleet` skill link in that agent's skills directory.
    That consent coupling is the feature's default switch, so it is stated
    here at the point of decision (see HOST_READER_SOURCE_GATE). To keep a
    skill link maintained WITHOUT enabling sync or usage reading, use
    `mm install-skills --agent <key>` instead. This command does not
    install the link itself.
    """
    config = _get_config()
    try:
        _validate_source_name(name, config, force=force)
    except ConfigError as e:
        _error(str(e))

    sync = dict(config.get("sync", {}) or {})
    disabled = list(sync.get("disabled_sources", []) or [])
    explicit_sources = list(sync.get("sources", []) or [])
    has_explicit_sources = "sources" in sync
    explicit_names = [s["name"] for s in explicit_sources]
    default_names = [s["name"] for s in DEFAULT_SOURCES]

    updates: dict[str, Any] = {}

    if name in disabled:
        disabled.remove(name)
        updates["disabled_sources"] = sorted(disabled)

    # Grok must be materialized even when its customization dirs are not
    # present yet: explicit enable is consent.  In a legacy config, first
    # preserve the resolved source set before writing sync.sources.  An
    # explicit list takes priority over legacy claude_dir/default fallback;
    # writing only Grok here would make the existing sources disappear and
    # eventually produce their deletion tombstones.
    if name == "grok" and not has_explicit_sources:
        explicit_sources = get_sources(config)
        explicit_names = [s["name"] for s in explicit_sources]
        updates["sources"] = explicit_sources

    # If the user has explicit sources and this name isn't among them,
    # but it IS in DEFAULT_SOURCES, append the default so enable actually
    # has effect (auto-detect doesn't fire when explicit sources are set).
    needs_explicit_append = name not in explicit_names and (
        name == "grok" or (explicit_sources and name in default_names)
    )
    if needs_explicit_append:
        default = get_default_source(name)
        if default is not None:
            explicit_sources.append(default)
            updates["sources"] = explicit_sources

    if not updates:
        # Already enabled and configured — no-op message.
        if name in explicit_names or (not explicit_sources and name in default_names):
            if name == "grok":
                _set_grok_host_usage(config, enabled=True, quiet=True)
            console.print(f"[dim]Source '{name}' is already enabled.[/dim]")
            return

    if updates:
        config_updates: dict[str, dict[str, Any]] = {"sync": updates}
        if name == "grok":
            config_updates["retro"] = {"grok_host_usage": True}
        patch_config_on_disk(config_updates)

    if name == "grok" and not updates:
        _set_grok_host_usage(config, enabled=True, quiet=True)

    _record_seen([name])
    console.print(f"[green]Enabled source '{name}' on this device.[/green]")
    if name == "grok":
        console.print(
            "[dim]Syncs ~/.grok skills/, commands/, and rules/ only. "
            "Session files are not synced. Prompts never leave the Mac.[/dim]"
        )
        console.print(
            "[dim]Reads terminal token totals from local Grok session updates. "
            "Session files are not synced. Prompts never leave the Mac.[/dim]"
        )
        console.print("[dim]Run 'mm disable-source grok' to turn this off.[/dim]")


@app.command(name="reconfigure-sources")
def reconfigure_sources() -> None:
    """Re-run the source picker against the current config + new defaults.

    Use this after `mm` ships a new source (e.g. codex in v0.11+) to walk
    through enable/disable for every known source. Preserves user
    customizations on existing [[sync.sources]] entries (include_dirs,
    exclude_patterns).

    Atomicity: Ctrl-C mid-prompt aborts without writing. The whole
    reconfigured state is committed in a single patch_config_on_disk call.
    """
    config = _get_config()
    sync = dict(config.get("sync", {}) or {})
    explicit_sources = list(sync.get("sources", []) or [])
    explicit_names = [s["name"] for s in explicit_sources]
    disabled = list(sync.get("disabled_sources", []) or [])
    default_names = [s["name"] for s in DEFAULT_SOURCES]

    # Unified ordered list: DEFAULT_SOURCES first (canonical surfacing
    # order), then any user-customized names not in defaults. Prefer the
    # user's existing entry for each name so customizations show in the
    # picker context.
    seen_names: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for d in DEFAULT_SOURCES:
        seen_names.add(d["name"])
        existing = next((s for s in explicit_sources if s["name"] == d["name"]), None)
        ordered.append(existing if existing is not None else d)
    for s in explicit_sources:
        if s["name"] not in seen_names:
            seen_names.add(s["name"])
            ordered.append(s)

    if not ordered:
        console.print("[dim]No sources configured to reconfigure.[/dim]")
        return

    console.print("[bold]Reconfigure sources for this device.[/bold]")
    console.print(
        "[dim]Disabling marks the source in disabled_sources (per-machine, "
        "not synced). Disabling does NOT delete the [[sync.sources]] entry — "
        "re-enabling preserves customizations.[/dim]\n"
    )

    new_explicit_sources: list[dict[str, Any]] = []
    new_disabled: list[str] = []

    try:
        for item in ordered:
            iname = item["name"]
            detected = _source_path_is_detected(item)
            default_active = iname in default_names
            if iname == "grok":
                # A bare ~/.grok contains session state and credentials on
                # every Grok install. It must not become a default-Y source
                # or authorize the separate usage reader.
                default_active = detected
            currently_active = (
                iname in explicit_names or (not explicit_sources and default_active)
            ) and iname not in disabled
            if iname == "grok":
                # A pre-22B usage-only opt-in is durable consent.  Keep it
                # default-on in this explicit reconfigure flow even when the
                # Grok customization dirs are absent, unless the user says N.
                currently_active = currently_active or grok_host_usage_enabled(config)
            answered = _prompt_source_toggle(
                item, current_state=currently_active, detected=detected
            )
            existing = next((s for s in explicit_sources if s["name"] == iname), None)
            if answered:
                # Source on: ensure in [[sync.sources]] (use existing for
                # customizations, else default).
                if existing is not None:
                    new_explicit_sources.append(existing)
                else:
                    default = get_default_source(iname)
                    if default is not None:
                        new_explicit_sources.append(default)
            else:
                # Source off: keep [[sync.sources]] entry intact for re-enable
                # to preserve customizations, plus mark disabled.
                if existing is not None:
                    new_explicit_sources.append(existing)
                new_disabled.append(iname)
    except (KeyboardInterrupt, typer.Abort):
        console.print("\n[yellow]Reconfigure aborted; no changes written.[/yellow]")
        raise typer.Exit(1) from None

    updates: dict[str, Any] = {"disabled_sources": sorted(new_disabled)}
    if new_explicit_sources:
        updates["sources"] = new_explicit_sources

    grok_on = (
        any(s.get("name") == "grok" for s in new_explicit_sources) and "grok" not in new_disabled
    )
    patch_config_on_disk({"sync": updates, "retro": {"grok_host_usage": grok_on}})

    # Mark every name as seen — reconfigure is the explicit acknowledgment
    # surface, so even sources the user re-confirmed should not surface as
    # "new" hints next run.
    _record_seen([item["name"] for item in ordered])

    console.print("\n[green]Source configuration updated.[/green]")


# ── migrate-config ────────────────────────────────────────────────────


def _compute_recommended_excludes_diff(
    sources: list[dict],
) -> list[tuple[str, list[str], list[str]]]:
    """For each user-configured source, compute the missing recommended globs.

    Returns [(source_name, missing_globs, current_globs)] only for sources
    that (a) match a DEFAULT_SOURCES entry by name AND (b) the default
    declares `exclude_patterns` AND (c) at least one default glob is absent
    from the user's current list. Idempotent: a source already containing
    every recommended glob is omitted.
    """
    diffs: list[tuple[str, list[str], list[str]]] = []
    for src in sources:
        default = get_default_source(src.get("name", ""))
        if default is None:
            continue
        recommended = default.get("exclude_patterns") or []
        if not recommended:
            continue
        current: list[str] = list(src.get("exclude_patterns") or [])
        missing = [p for p in recommended if p not in current]
        if missing:
            diffs.append((src["name"], missing, current))
    return diffs


def _config_missing_recommended_excludes(config: dict) -> list[str]:
    """Names of explicit `[[sync.sources]]` entries missing recommended excludes.

    Returns [] when there's no explicit `sync.sources` array (legacy
    claude_dir-only configs and bare configs use DEFAULT_SOURCES verbatim,
    which already includes the recommended excludes — nothing to migrate).
    """
    sources = config.get("sync", {}).get("sources")
    if not sources:
        return []
    return [name for name, _missing, _current in _compute_recommended_excludes_diff(sources)]


def _explicit_opencode_source_present(sources: list[dict] | None) -> bool:
    """True when an explicit ``[[sync.sources]]`` entry is named ``opencode``.

    Track 37B dropped that name from ``DEFAULT_SOURCES``. A leftover block
    is a user-defined generic source until ``mm migrate-config`` removes it
    and records the name in ``disabled_sources`` (the consumer-boundary
    filter that keeps ``generate_tombstones`` from minting a deletion
    tombstone for every file the source ever pushed).
    """
    if not sources:
        return False
    return any(s.get("name") == "opencode" for s in sources)


def _migrate_config_core(*, yes: bool, dry_run: bool) -> None:
    """Body of `mm migrate-config`. Extracted so the interactive
    `_maybe_prompt_migration` can call it directly without going through
    typer's option-parsing machinery.
    """
    config = _get_config()

    sources = config.get("sync", {}).get("sources")
    if not sources:
        console.print(
            "[dim]Config has no explicit [[sync.sources]] entries — "
            "nothing to migrate. DEFAULT_SOURCES already include the "
            "recommended excludes.[/dim]"
        )
        return

    diffs = _compute_recommended_excludes_diff(sources)
    retire_opencode = _explicit_opencode_source_present(sources)
    if not diffs and not retire_opencode:
        console.print("[green]Config is already up to date.[/green]")
        return

    if diffs:
        console.print("\n[bold]Recommended exclude_patterns updates:[/bold]")
        for name, missing, current in diffs:
            console.print(f"\n  [bold]{name}[/bold]  (current: {current!r})")
            for p in missing:
                console.print(f"    [green]+ {p}[/green]")
    if retire_opencode:
        console.print("\n[bold]Retired source:[/bold]")
        console.print(
            "  [bold]opencode[/bold] — remove the leftover [[sync.sources]] "
            "block and record the name in disabled_sources so the next "
            "push does not mint deletion tombstones."
        )

    if dry_run:
        console.print("\n[dim]Dry run — no changes written.[/dim]")
        return

    if not yes and not typer.confirm("\nApply these updates?", default=True):
        console.print("[yellow]Cancelled.[/yellow]")
        return

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    try:
        diff_map = {name: missing for name, missing, _current in diffs}
        new_sources: list[dict] = []
        for src in sources:
            if src.get("name") == "opencode":
                continue
            if src.get("name") in diff_map:
                merged = dict(src)
                current = list(src.get("exclude_patterns") or [])
                merged["exclude_patterns"] = current + diff_map[src["name"]]
                new_sources.append(merged)
            else:
                new_sources.append(src)

        # One patch for both halves. patch_config_on_disk is section-level
        # shallow and REPLACES sync.sources wholesale; disabled_sources is
        # a sibling field in the same section. The disabled_sources write
        # is what feeds _filter_disabled_sources so generate_tombstones
        # sees an empty diff for the retired name.
        sync_updates: dict[str, Any] = {"sources": new_sources}
        if retire_opencode:
            disabled = list(config.get("sync", {}).get("disabled_sources", []) or [])
            if "opencode" not in disabled:
                disabled.append("opencode")
            sync_updates["disabled_sources"] = sorted(disabled)
        patch_config_on_disk({"sync": sync_updates})
        # Clear any prior migration breadcrumb — config now matches.
        breadcrumb = _migration_state_path()
        try:
            breadcrumb.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        parts: list[str] = []
        if diffs:
            parts.append(f"Updated {len(diffs)} source(s)")
        if retire_opencode:
            parts.append("removed the retired opencode source block")
        console.print(
            f"[green]{'; '.join(parts)}.[/green] Config written to {_config_module.CONFIG_PATH}."
        )
    finally:
        release_lock()


@app.command(name="migrate-config")
def migrate_config(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the diff without writing config.toml."
    ),
) -> None:
    """Add recommended `exclude_patterns` to existing `[[sync.sources]]`.

    Compares each user-configured source against the matching DEFAULT_SOURCES
    entry and proposes adding any missing recommended globs. Also removes a
    leftover ``opencode`` source block (retired in v0.12.55) and records the
    name in ``disabled_sources``. Idempotent — re-running on a fully-migrated
    config exits with "already up to date".

    Acquires the mm lockfile so a concurrent push/pull can't read a half-
    written config.
    """
    _migrate_config_core(yes=yes, dry_run=dry_run)


# ── install-skills ────────────────────────────────────────────────────


@app.command(name="install-skills")
def install_skills_cmd(
    agent: list[str] = typer.Option(
        [],
        "--agent",
        help=(
            "Grant and persist skill-link maintenance for this agent key, then "
            "install every authorized agent (not only this one). Does not enable "
            "source sync or usage reading. Repeatable. Requires an existing "
            "config (run mm init first). Bare invocation (no --agent): with a "
            "config, install what is already authorized; with no config, install "
            "for every available agent (fresh-machine setup)."
        ),
    ),
) -> None:
    """Install (or re-install) retro-fleet for every authorized agent.

    Prints one line per agent, including agents that aren't installed.

    Force-runs the same self-heal that ``mm init`` and ``mm push`` invoke
    automatically, bypassing the steady-state TTL gate. Intended for:

    * post-cleanup recovery (link removed by hand, e.g. after deleting an
      old pipx workspace whose path the link pointed at)
    * manual install on a machine where ``mm push`` hasn't run yet
    * verifying the link state on a fresh ``pipx install`` of mm
    * granting skill-link maintenance for one agent without enabling that
      agent's sync source or usage reader (``--agent KEY``)

    Each agent link points at the mm-owned store
    ``~/.local/share/mind-meld/agent-skills/retro-fleet/``. ``mm`` copies
    ``SKILL.md`` there from the running package and refreshes it on a
    version-then-hash compare. ``pipx upgrade`` no longer updates the
    agent-visible file in place.
    """
    config_path = _config_module.CONFIG_PATH
    known = {row.key for row in skill_link.AGENT_ROWS}
    known_list = ", ".join(row.key for row in skill_link.AGENT_ROWS)
    requested = [key.strip() for key in agent if key.strip()]

    if requested:
        unknown = [key for key in requested if key not in known]
        if unknown:
            typer.echo(
                f"mm: error: unknown agent '{unknown[0]}'; known agents: {known_list}",
                err=True,
            )
            raise typer.Exit(code=1)
        if not config_path.exists():
            typer.echo(
                "mm: error: --agent needs a config to record the grant. "
                "Run 'mm init' first, then retry this command. "
                "No agent links were changed.",
                err=True,
            )
            raise typer.Exit(code=1)
        try:
            cfg = load_config()
            sources = get_sources(cfg)
        except MindMeldError as e:
            typer.echo(
                f"mm: error: could not read config: {safe_str(e)}. "
                "No agent links were changed. Run mm diag "
                "(it still works with a broken config).",
                err=True,
            )
            raise typer.Exit(code=1) from e
        order = {row.key: i for i, row in enumerate(skill_link.AGENT_ROWS)}
        effective_before = skill_link.consented_agent_keys(cfg, sources)
        skills = cfg.get("skills")
        unknown_explicit_agents = (
            [key for key in skills.get("agents", []) if key not in known]
            if isinstance(skills, dict)
            else []
        )
        new_agents = sorted(effective_before | set(requested), key=lambda k: order[k])
        new_agents.extend(unknown_explicit_agents)
        try:
            patch_config_on_disk(
                {"skills": {"maintain_links": True, "agents": new_agents}},
                config_path,
            )
        except (ConfigError, OSError, StorageError) as e:
            typer.echo(
                f"mm: error: could not write {config_path}: {e}. "
                "No agent links were changed. Retry this command.",
                err=True,
            )
            raise typer.Exit(code=1) from e
        try:
            cfg = load_config()
            sources = get_sources(cfg)
        except MindMeldError as e:
            typer.echo(
                f"mm: error: could not read config: {safe_str(e)}. "
                "No agent links were changed. Run mm diag "
                "(it still works with a broken config).",
                err=True,
            )
            raise typer.Exit(code=1) from e
        may_create = skill_link.consented_agent_keys(cfg, sources)
        typer.echo(
            f"Recorded skill-link grant in {config_path}: "
            f"maintain_links = true, agents = [{', '.join(new_agents)}]. "
            "This command installs every authorized agent, not only the ones named here."
        )
    elif not config_path.exists():
        may_create = None
    else:
        try:
            cfg = load_config()
            sources = get_sources(cfg)
            may_create = skill_link.consented_agent_keys(cfg, sources)
        except MindMeldError as e:
            typer.echo(
                f"mm: error: could not read config: {safe_str(e)}. "
                "No agent links were changed. Run mm diag "
                "(it still works with a broken config).",
                err=True,
            )
            raise typer.Exit(code=1) from e

    try:
        results = skill_link._ensure_retro_skill_links(
            dry_run=False, explicit=True, may_create=may_create
        )
    except Exception as e:
        typer.echo(
            f"mm: error: retro-fleet skill installation failed: "
            f"{type(e).__name__}: {safe_str(e)}. Run mm diag.",
            err=True,
        )
        raise typer.Exit(code=1) from e

    available = False
    failed = False

    for result in results:
        descriptor = result.descriptor
        target = safe_str(str(result.target))
        agent_root = safe_str(str(descriptor.agent_root))
        dest = (
            safe_str(str(result.link_target))
            if result.link_target is not None
            else (safe_str(str(result.skill_src)) if result.skill_src is not None else "unknown")
        )
        if result.status == "installed":
            available = True
            typer.echo(f"Installed: {descriptor.display_name}: {target} -> {dest}")
        elif result.status == "unchanged":
            available = True
            typer.echo(
                f"Installed (already correct): {descriptor.display_name}: {target} -> {dest}"
            )
        elif result.status == "unavailable":
            typer.echo(f"Unavailable: {descriptor.display_name} ({agent_root} is absent)")
        elif result.status == "declined":
            available = True
            typer.echo(f"Skipped: {skill_link.render_skill_status(result)}")
        elif result.status == "removed-by-user":
            # Unreachable while this command passes explicit=True (the guard in
            # _install_available_skill_target is skipped). Handled anyway: the
            # bare `else` below reports "installation failed: None" and exits 1,
            # so any future non-explicit caller would turn a benign outcome into
            # a hard failure.
            available = True
            typer.echo(f"Left removed: {skill_link.render_skill_status(result)}")
        elif result.status in ("dangling-ours", "dangling-ours-legacy", "foreign"):
            available = True
            failed = True
            typer.echo(
                f"mm: error: {descriptor.display_name}: {skill_link.render_skill_status(result)}",
                err=True,
            )
        else:
            available = True
            failed = True
            typer.echo(
                f"mm: error: {descriptor.display_name}: {target} installation failed: "
                f"{result.reason}",
                err=True,
            )

    statuses = {result.status for result in results}
    if statuses <= {"declined", "unavailable"} and "declined" in statuses:
        example = skill_link.AGENT_ROWS[0].key
        typer.echo(
            "No agent is enabled for skill install. "
            f"Enable one: mm install-skills --agent {example} "
            "(repeat --agent for each other link target). Inspect with: mm diag"
        )
        raise typer.Exit(code=0)
    if not available:
        typer.echo(
            "mm: error: no supported agent skills directory exists; install an agent first",
            err=True,
        )
        raise typer.Exit(code=1)
    if failed:
        raise typer.Exit(code=1)


# ── retro-fleet ───────────────────────────────────────────────────────


@app.command(name="retro-fleet")
def retro_fleet_cmd(
    window: str = typer.Argument("7d", help="Retro window (Nd, e.g. '7d', '30d'). Days only."),
    no_author_filter: bool = typer.Option(
        False,
        "--no-author-filter",
        help="Disable author-email filter; render ALL fleet commits.",
    ),
    theme: list[str] = typer.Option(
        [],
        "--theme",
        help=(
            "TOP WORK theme line for the ASCII card (pass up to 3 times). "
            "Supplied by the retro-fleet skill on the second pass."
        ),
    ),
    noteworthy: str = typer.Option(
        "",
        "--noteworthy",
        help="NOTEWORTHY line for the ASCII card. Supplied by the skill.",
    ),
    name: str = typer.Option(
        "", "--name", help="Optional name in the ASCII card header (e.g. 'kb')."
    ),
    no_save: bool = typer.Option(
        False,
        "--no-save",
        hidden=True,
        help="Deprecated no-op. Snapshot persistence was removed in v0.12.39.",
    ),
    dump_host_usage: bool = typer.Option(
        False,
        "--dump-host-usage",
        help="Forensic JSON of accepted host inventory. Skips the markdown retro.",
    ),
) -> None:
    """Render fleet retrospective markdown to stdout.

    Primarily invoked by the ``retro-fleet`` Claude Code skill — direct CLI
    use is fine for scripted exports (``mm retro-fleet 30d > /tmp/retro.md``)
    but loses the LLM judgment layer (natural-language window parsing,
    error-translation) the skill provides.

    Two-pass shape (v0.12.0+):
    * Pass 1 — ``mm retro-fleet 7d`` emits the full markdown body plus a
      ``MM_THEMES_PROMPT`` JSON sidecar at the bottom. Skill reads the
      sidecar, synthesizes themes + noteworthy, then re-invokes.
    * Pass 2 — ``mm retro-fleet 7d --theme A --theme B --theme C
      --noteworthy "..." --name kb`` re-renders with a pixel-aligned
      ASCII card up top. ``--no-save`` is accepted as a hidden no-op
      (removed as of v0.12.39; kept so a stale skill copy still exits 0).

    Thin wrapper around ``mind_meld.skills.retro_fleet.aggregator.main``.
    Routes through ``mm`` (guaranteed on PATH wherever mm is installed)
    instead of the prior ``python -m mind_meld.skills.retro_fleet.aggregator``
    invocation, which assumed a ``python`` executable on PATH with mm
    importable — false on macOS systems where only ``python3`` is on PATH,
    and structurally false for pipx installs whose ``mm`` lives in an
    isolated venv that nothing else sees.
    """
    from mind_meld.skills.retro_fleet.aggregator import main as _aggregator_main

    argv = [window]
    if no_author_filter:
        argv.append("--no-author-filter")
    for t in theme:
        argv.extend(["--theme", t])
    if noteworthy:
        argv.extend(["--noteworthy", noteworthy])
    if name:
        argv.extend(["--name", name])
    if no_save:
        argv.append("--no-save")
    if dump_host_usage:
        argv.append("--dump-host-usage")
    raise typer.Exit(code=_aggregator_main(argv))


# ── recapture ─────────────────────────────────────────────────────────


RECAPTURE_EXIT_PARTIAL = 4
"""Partial recapture exit. Distinct from pull ``--conflict-mode fail`` (3)."""


def _parse_recapture_window(window: str) -> int:
    days = events.parse_nd_window(window)
    if days is None:
        _error(events.window_syntax_error(window))
    if days < events.RECAPTURE_WINDOW_MIN_DAYS or days > events.RECAPTURE_WINDOW_MAX_DAYS:
        _error(
            f"WINDOW must be between {events.RECAPTURE_WINDOW_MIN_DAYS}d and "
            f"{events.RECAPTURE_WINDOW_MAX_DAYS}d; got {days}d. "
            "The limit keeps the synced event log bounded."
        )
    return days


def _recapture_commit_stats(
    git_rows: list[dict], events_dir: Path
) -> tuple[list[dict], list[dict]]:
    already = events.recorded_commit_keys(events_dir)
    records = []
    for row in git_rows:
        records.extend(events.git_row_commit_records(row))
    new = [c for c in records if (c.get("remote", ""), c.get("sha", "")) not in already]
    return records, new


def _oldest_commit_date(commits: list[dict]) -> datetime | None:
    oldest: datetime | None = None
    for commit in commits:
        raw = commit.get("date")
        if not isinstance(raw, str):
            continue
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if oldest is None or ts < oldest:
            oldest = ts
    return oldest


@app.command()
def recapture(
    window: str = typer.Argument(
        events.RECAPTURE_WINDOW_DEFAULT,
        help=(
            "Redo this Mac's git capture for WINDOW (default 30d, same as mm init). "
            "Safe to re-run — commits dedup fleet-wide on (remote, sha). "
            "Retros window by the COMMIT's date, not by when mm captured it, "
            "so mm recapture 90d then mm retro-fleet 7d will not show June commits."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Discover and walk, then report; write and upload nothing.",
    ),
) -> None:
    """Recover omitted git commits into the fleet retro.

    Writes git-snapshot rows first, then runs the ordinary push path so the
    substantive-change gate passes without a special case. Partial recovery
    exits 4. Zero discovered repositories writes nothing and exits 1.
    """
    days = _parse_recapture_window(window)
    config = _get_config()
    _maybe_prompt_migration(config)
    config = _get_config()
    passphrase = _get_passphrase_or_exit()

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    try:
        backend = get_backend(config)
        try:
            memory_kb = _init_crypto_session(backend, passphrase, config)
        except MindMeldError as e:
            _error(str(e))
        sources = get_sources(config)
        disabled = list(config.get("sync", {}).get("disabled_sources", []) or [])
        if "mm-events" in disabled or not any(s.get("name") == "mm-events" for s in sources):
            stderr_console.print(
                "[red]Error:[/red] recapture requires the 'mm-events' source, "
                "but it is disabled on this Mac."
            )
            stderr_console.print("Fix: mm enable-source mm-events")
            stderr_console.print(f"Then retry: mm recapture {window}")
            raise typer.Exit(1)

        since = datetime.now(timezone.utc) - timedelta(days=days)
        prepared = events_tail._prepare_recapture(
            config, sources, config["device"]["id"], since=since
        )
        if prepared is None:
            stderr_console.print(
                "[red]Error:[/red] recapture requires the 'mm-events' source, "
                "but it is disabled on this Mac."
            )
            stderr_console.print("Fix: mm enable-source mm-events")
            stderr_console.print(f"Then retry: mm recapture {window}")
            raise typer.Exit(1)

        n_roots = len(prepared.root_discovery.roots)
        skipped = prepared.walk_budget_aborts + prepared.walk_errors
        records, new_records = _recapture_commit_stats(prepared.git_rows, prepared.events_dir)
        until = prepared.until
        since_day = prepared.since.date().isoformat()
        until_day = until.date().isoformat()

        if dry_run:
            row_bytes = sum(
                len(json.dumps(row, sort_keys=True).encode("utf-8")) for row in prepared.git_rows
            )
            console.print("[bold]Recapture dry-run[/bold] — nothing written.")
            console.print(f"  Window scanned:   {since_day} → {until_day} ({days}d)")
            console.print(f"  Repositories:     {n_roots} scanned, {skipped} skipped")
            console.print(f"  Commit records:   {len(records)} captured")
            console.print(f"  Estimated size:   {row_bytes} bytes")
            if n_roots == 0:
                raise typer.Exit(1)
            return

        if n_roots == 0:
            console.print(
                "Recapture stopped: no Git repositories were discovered on this Mac. "
                "Add their absolute paths under [retro].repo_roots, verify with "
                "'mm diag', then retry."
            )
            raise typer.Exit(1)

        device_id = config["device"]["id"]
        events.write_push_event(prepared.events_dir, device_id, prepared.git_rows)

        try:
            result = _push_core(config, passphrase, memory_kb, verbose=False, dry_run=False)
        except typer.Exit:
            console.print(
                "Recapture was written locally but not synced. "
                "Fix the storage error above, then run 'mm push'."
            )
            raise

        synced = result is not None
        if not synced:
            console.print(
                "Recapture was written locally but not synced. "
                "Fix the storage error above, then run 'mm push'."
            )
            raise typer.Exit(1)

        partial = (
            prepared.root_discovery.exceeded or bool(prepared.root_discovery.errors) or skipped > 0
        )
        n_scanned_ok = n_roots - skipped
        if partial:
            skip_reason = "the Git walk exceeded its budget"
            if prepared.walk_errors and not prepared.walk_budget_aborts:
                skip_reason = "the Git walk failed for those repositories"
            elif prepared.root_discovery.exceeded:
                skip_reason = "git repository discovery was incomplete"
            narrower = "7d" if days > 7 else "1d"
            noun = "repository was" if skipped == 1 else "repositories were"
            console.print(
                f"Recapture incomplete: captured {len(records)} commit records "
                f"from {n_scanned_ok} of {n_roots} repositories for "
                f"{since_day} → {until_day}. {skipped} {noun} skipped because "
                f"{skip_reason}. Retry a narrower window: mm recapture {narrower}"
            )
            raise typer.Exit(RECAPTURE_EXIT_PARTIAL)

        if not records:
            console.print(
                f"Recapture complete: scanned {n_roots} repositories but found "
                f"no commits dated {since_day} → {until_day}. "
                "No retro output will change."
            )
            return

        if not new_records:
            console.print(
                "Recapture found no commits that were not already recorded. "
                f"This Mac's events log already covers {since_day} → {until_day} "
                f"for all {n_roots} repositories it can see.\n"
                "If the retro is still short, the gap is on another Mac "
                "(mm devices) or in a repo discovery cannot see "
                "(mm diag, then [retro] repo_roots)."
            )
            return

        oldest = _oldest_commit_date(new_records)
        cover_days = days
        if oldest is not None:
            cover_days = max(days, (until.date() - oldest.date()).days + 1)
            cover_days = min(cover_days, events.RECAPTURE_WINDOW_MAX_DAYS)
        console.print("[bold green]Recapture complete.[/bold green]")
        console.print(f"  Window scanned:   {since_day} → {until_day} ({days}d)")
        console.print(f"  Repositories:     {n_roots} scanned, {skipped} skipped")
        console.print(f"  Commit records:   {len(records)} captured")
        console.print(f"  Synced:           {'yes' if synced else 'no'}")
        console.print("")
        console.print("Retros window by the COMMIT's date, not by when mm captured it, so a short")
        console.print("window will not show the older recoveries:")
        console.print(f"  mm retro-fleet {cover_days}d   # covers everything just recaptured")
        console.print("  mm retro-fleet 7d    # covers the last 7 days only")
    finally:
        release_lock()

    upgrade.emit_nudge_if_due(config)


# ── refresh-identity ──────────────────────────────────────────────────


@app.command(name="refresh-identity")
def refresh_identity_cmd(
    json_output: bool = typer.Option(
        False, "--json", help="Emit the resulting email list as JSON to stdout."
    ),
) -> None:
    """Force-refresh the local identity cache.

    The cache feeds the ``local_emails`` field on every ``mm push``
    event (v0.11.17+), which the retro-fleet aggregator unions across
    peers to build a fleet-wide author-email trust set. The cache
    refreshes itself on a 7-day TTL automatically; this subcommand is the
    explicit knob for picking up identity changes immediately:

    * after editing ``[retro].author_emails`` in mm ``config.toml``
    * after ``gh auth login`` / changing the GitHub CLI account
    * after editing ``git config --global user.email``
    * after adding a per-repo ``user.email`` override

    Sources unioned: global git config, per-repo git config (every
    discovered git root, bounded by a 5s wall-clock budget), mm's
    ``[retro].author_emails`` config knob, and the GitHub
    ``<id>+<login>@users.noreply.github.com`` form when ``gh`` is
    authenticated. A failed source contributes nothing — the cache still
    rebuilds with what was reachable.
    """
    emails = identity.refresh_identity_cache(force=True)
    if json_output:
        typer.echo(json.dumps(sorted(emails)))
        return
    if not emails:
        typer.echo(
            "mm: warning: no author emails resolved — check `git config "
            "--global user.email`, `gh auth status`, and "
            "`[retro].author_emails` in your mm config",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"Refreshed identity cache: {len(emails)} email(s)")
    for e in emails:
        typer.echo(f"  {e}")


# ── log ───────────────────────────────────────────────────────────────


_LogAction = Literal[
    "written",
    "merged",
    "merged-via-lcs",
    "skipped",
    "conflicted",
    "excluded",
    "uploaded",
    "failed",
]
_LogVerb = Literal["pull", "push", "self-upgrade"]


@app.command(name="log")
def log_cmd(
    source: str | None = typer.Option(
        None, "--source", help="Filter by source name (e.g. 'claude', 'gstack')."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Only entries at/after this ISO-8601 timestamp."
    ),
    action: _LogAction | None = typer.Option(
        None,
        "--action",
        help="Filter by per-file action.",
        case_sensitive=False,
    ),
    verb: _LogVerb | None = typer.Option(
        None,
        "--verb",
        help="Filter by pull or push.",
        case_sensitive=False,
    ),
    limit: int = typer.Option(
        50, "--limit", "-n", help="Show at most N records (most recent first)."
    ),
    fmt: str = typer.Option(
        "table",
        "--format",
        help="Output format: 'table' (human) or 'jsonl' (machine).",
        case_sensitive=False,
    ),
) -> None:
    """Query the pull/push history log.

    Records every per-file pull and push action to
    ~/.config/mind-meld/pull-history.jsonl. Useful for "what conflicted on
    date X" audits even after the .sync-conflict-* files are resolved
    or reaped, and for "what is my exclude_patterns actually filtering"
    via `mm log --action excluded`.
    """
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            _error(f"--since: not a valid ISO-8601 timestamp: {since!r}")
            return  # unreachable
    else:
        since_dt = None

    fmt_lower = fmt.lower()
    if fmt_lower not in ("table", "jsonl"):
        _error(f"--format must be 'table' or 'jsonl', got {fmt!r}")
        return  # unreachable

    rows: list[dict] = []
    for rec in pullhistory.read_records():
        if source and rec.get("source") != source:
            continue
        if action and rec.get("action") != action:
            continue
        if verb and rec.get("verb") != verb:
            continue
        if since_dt is not None:
            try:
                rec_ts = datetime.fromisoformat(rec.get("ts", ""))
            except (TypeError, ValueError):
                continue
            if rec_ts.tzinfo is None:
                rec_ts = rec_ts.replace(tzinfo=timezone.utc)
            if rec_ts < since_dt:
                continue
        rows.append(rec)

    # Most-recent-first; cap to limit.
    rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
    if limit > 0:
        rows = rows[:limit]

    if not rows:
        if fmt_lower == "jsonl":
            return
        console.print("[dim]No log entries.[/dim]")
        return

    if fmt_lower == "jsonl":
        import json as _json

        for r in rows:
            typer.echo(_json.dumps(r, sort_keys=True))
        return

    table = Table(title=f"mm log ({len(rows)} entries)")
    table.add_column("ts", overflow="fold")
    table.add_column("verb")
    table.add_column("action")
    table.add_column("source")
    table.add_column("path", overflow="fold")
    # `extra` column carries self-upgrade row details ("0.9.3 → 0.9.4")
    # without overloading the source/path columns. Empty for pull/push
    # rows. D8 locked this shape per Codex outside voice.
    table.add_column("extra", overflow="fold")
    for r in rows:
        verb = r.get("verb", "?")
        if verb == "self-upgrade":
            old = r.get("old_version", "?")
            new = r.get("new_version", "?")
            table.add_row(
                r.get("ts", "?"),
                verb,
                "—",
                "—",
                "—",
                f"{old} → {new}",
            )
        else:
            table.add_row(
                r.get("ts", "?"),
                verb,
                r.get("action", "?"),
                r.get("source", "?"),
                r.get("rel_path", "?"),
                "",
            )
    console.print(table)


# ── conflicts / resolve ───────────────────────────────────────────────


@app.command()
def conflicts() -> None:
    """List .sync-conflict-* files across all synced sources.

    Per-row column meaning depends on the filename prefix:
      * `v0-` (pre-inversion, pre-v0.9.2 conflict file) — sidecar holds
        LOCAL bytes; canonical holds REMOTE.
      * no prefix (post-inversion, v0.9.2+) — canonical holds LOCAL;
        sidecar holds REMOTE.

    Read-only: never mutates conflict file names. Migration to the `v0-`
    prefix happens lock-protected in `mm pull` and `mm resolve` only —
    `mm conflicts` is lockless and any rename here would race autopull.
    """
    config = _get_config()
    hits = resolveflow._find_conflict_files(config)
    if not hits:
        console.print("[green]No conflict files.[/green]")
        return

    table = Table(title=f"Conflict files ({len(hits)})")
    table.add_column("Source")
    table.add_column("Mode")
    table.add_column("local", no_wrap=False, overflow="fold")
    table.add_column("remote", no_wrap=False, overflow="fold")
    table.add_column("Conflict age")
    table.add_column("Peer edit age")
    now = datetime.now(timezone.utc)
    pre_inversion_seen = False
    for src_name, cpath, canonical in sorted(hits, key=lambda h: str(h[1])):
        created = parse_conflict_created_at(cpath.name)
        if created is None:
            conflict_age = "?"
        else:
            conflict_age = format_age_delta((now - created).total_seconds())
        try:
            mtime = datetime.fromtimestamp(cpath.stat().st_mtime, tz=timezone.utc)
            peer_age = format_age_delta((now - mtime).total_seconds())
        except OSError:
            peer_age = "?"
        is_pre = is_pre_inversion_conflict_filename(cpath.name)
        if is_pre:
            pre_inversion_seen = True
            mode = "[yellow]pre-v0.9.2[/yellow]"
            # Pre-inversion: sidecar = local, canonical = remote.
            local_display = safe_str(cpath)
            remote_display = safe_str(canonical) if canonical else "[dim](gone)[/dim]"
        else:
            mode = "v0.9.2+"
            # Post-inversion: canonical = local, sidecar = remote.
            local_display = safe_str(canonical) if canonical else "[dim](gone)[/dim]"
            remote_display = safe_str(cpath)
        table.add_row(src_name, mode, local_display, remote_display, conflict_age, peer_age)
    console.print(table)
    console.print(
        "\nRun [bold]mm resolve[/bold] to keep local, remote, or both "
        "interactively, or delete files manually with [bold]rm[/bold]."
    )
    if pre_inversion_seen:
        console.print(
            "\n[dim]Pre-v0.9.2 conflict files are listed in the table — "
            "run [bold]mm resolve[/bold] to migrate them to the new "
            "filename convention before resolving.[/dim]"
        )


# ── recover ───────────────────────────────────────────────────────────


def _quarantine_corrupt_manifest(
    backend: LocalBackend,
    storage_root: Path,
    device_id: str,
) -> Path:
    """Crash-durable move of a corrupt manifest blob to a quarantine sibling.

    Uses read-then-atomic-write-then-unlink (matching the discipline at
    storage/local.py:45 and sidecar.py:54) rather than plain os.rename.
    A power loss between steps leaves the source intact or the destination
    fully written — never both gone.

    Collision handling: if `<key>.corrupt-<ts>` already exists (a second
    quarantine within the same second), append a 4-char random suffix.

    Returns the quarantine path.
    """
    mkey = manifest_key(device_id)
    src = storage_root / mkey
    if not src.exists():
        raise FileNotFoundError(str(src))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    candidates = [
        src.with_name(src.name + f".corrupt-{ts}"),
        src.with_name(src.name + f".corrupt-{ts}-{secrets.token_hex(2)}"),
    ]
    dst = next((c for c in candidates if not c.exists()), None)
    if dst is None:
        # Both collided — extremely improbable, but pick a guaranteed-unique name.
        dst = src.with_name(src.name + f".corrupt-{ts}-{secrets.token_hex(4)}")

    data = src.read_bytes()
    # atomic_write_bytes with fsync=True ensures dst is durably written
    # before we unlink src.
    fsutil.atomic_write_bytes(dst, data, fsync=True)
    os.unlink(src)
    # Also fsync the parent directory so the unlink is durable. Best-effort
    # — the file content is already durable at dst. Worst-case on a crash
    # mid-fsync: src may reappear on next boot, but dst still has the
    # quarantined copy.
    try:
        fsutil.fsync_dir(src.parent)
    except (OSError, StorageError):
        pass
    return dst


@app.command()
def recover(
    abandon_manifest: bool = typer.Option(
        False,
        "--abandon-manifest",
        help=(
            "Quarantine this device's corrupt manifest and allow the next "
            "push to start fresh. DESTRUCTIVE: files deleted locally since "
            "the last successful push will lose their deletion records."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the typed-RESET confirmation (for scripted recovery).",
    ),
) -> None:
    """Last-resort escape hatch for corrupt-manifest recovery.

    Use ONLY when `mm push` refuses with "remote manifest corrupt, no
    local sidecar, and no peer manifests." Running this when the recovery
    chain has a viable source (sidecar or peers) is wasted destructiveness
    — push will self-heal through the normal chain.

    See SPEC.md "Manifest corruption recovery" / "Last-resort escape
    hatch" for the full contract.
    """
    if not abandon_manifest:
        _error(
            "mm recover requires a recovery mode flag. Today the only mode "
            "is --abandon-manifest (quarantine a corrupt manifest and allow "
            "the next push to start fresh)."
        )

    config = _get_config()
    passphrase = _get_passphrase_or_exit()
    device_id = config["device"]["id"]
    storage_path = config["storage"]["path"]
    storage_root = Path(storage_path).expanduser()

    backend = get_backend(config)
    try:
        memory_kb = _init_crypto_session(backend, passphrase, config)
    except MindMeldError as e:
        _error(str(e))

    # Refuse-when-healthy: if the normal recovery chain has any viable
    # source, this command has no business running.
    fetch = _fetch_remote_manifest(backend, device_id, passphrase, memory_kb)
    if fetch.is_ok:
        _error(
            "remote manifest is readable — recovery is not required. "
            "If you meant to investigate a different device's corrupt "
            "manifest, re-run mm push from that device."
        )
    if fetch.status == "missing":
        _error(
            "no manifest exists for this device at "
            f"{manifest_key(device_id)} — nothing to quarantine."
        )

    # Sidecar and peer tombstones are the non-destructive paths. If either
    # exists, refuse: running --abandon-manifest would lose fresh deletions
    # that the normal push recovery chain would preserve.
    sidecar_manifest = sidecar.read(device_id)
    if sidecar_manifest is not None:
        _error(
            f"local sidecar is present at {sidecar.sidecar_path()}. "
            f"Run 'mm push' — the normal recovery chain will use the "
            f"sidecar to preserve fresh deletions. --abandon-manifest "
            f"would throw those deletion records away."
        )
    peer_tombstones = _collect_peer_tombstones(backend, device_id, passphrase, memory_kb)
    if peer_tombstones:
        _error(
            f"peer manifests carry {len(peer_tombstones)} tombstone(s) that "
            f"'mm push' would use as a recovery source. Run 'mm push' instead "
            f"— --abandon-manifest would discard these tombstone records."
        )

    # Loud warning before the typed prompt. stderr_console so the UX is
    # consistent with other destructive paths (init BRICK).
    stderr_console.print(
        "\n[red]DANGER:[/red] this will QUARANTINE this device's corrupt "
        "manifest and allow the next push to start fresh with [bold]no "
        "prior-state knowledge[/bold]. Files you deleted locally since the "
        "last successful push will no longer propagate as deletions — peers "
        "will see those files come back on their next pull.\n"
    )
    _mkey_display = manifest_key(device_id)
    stderr_console.print(
        f"  Source blob:      {_mkey_display}\n"
        f"  Quarantine name:  {_mkey_display}.corrupt-<timestamp>\n"
        f"  Storage root:     {storage_root}\n"
    )

    if not yes:
        typed = typer.prompt('Type "RESET" (case-sensitive) to confirm and proceed')
        if typed != "RESET":
            stderr_console.print("[yellow]Aborted.[/yellow] Nothing changed.")
            raise typer.Exit(1)

    try:
        quarantine_path = _quarantine_corrupt_manifest(backend, storage_root, device_id)
    except FileNotFoundError:
        _error(f"{manifest_key(device_id)} not found on disk. Nothing to quarantine.")
    except OSError as e:
        _error(f"quarantine failed: {e}")

    console.print(f"[green]Quarantined[/green] corrupt manifest to [dim]{quarantine_path}[/dim].")
    console.print(
        "Next 'mm push' will start fresh with no prior-state manifest. "
        "The quarantined copy is preserved for post-mortem and can be "
        "deleted manually once you've confirmed recovery."
    )


@app.command()
def resolve(
    path: str | None = typer.Argument(
        None,
        help="Specific conflict path to resolve. If omitted, walks all conflicts.",
    ),
) -> None:
    """Interactively resolve .sync-conflict-* files.

    For each conflict: prints color LOCAL/REMOTE banners (with peer-name
    attribution when known), a 3-number divergence summary, the unified
    diff, then prompts:
      (l)ocal / (r)emote / (m)erge / (p)romote / (s)kip [default] / (a)bort.

    (l)ocal keeps your edits on this machine and discards the bytes from
    the other machine.
    (r)emote keeps the bytes from the other machine and discards your
    local edits on this conflict.
    (m)erge accepts the LCS-merged result over canonical (offered only
    when the content is text; never the default key -- you must type it).
    (p)romote keeps BOTH: renames the .sync-conflict-* sidecar to its own
    first-class filename so both versions survive as separate synced
    files. Use this when the two files turned out to be different
    documents that merely collided on a name.
    (s)kip leaves both files on disk; you can run `mm resolve` again
    later or delete the .sync-conflict-* file manually. Note: the next
    `mm pull` does NOT re-prompt unless remote changes again -- the
    .sync-conflict-* file persists until you act on it.
    (a)bort exits the resolve walk; previously-resolved conflicts stay
    resolved.

    Both deletions and renames propagate on the next `mm push` via the
    existing tombstone / additive-sync machinery.

    Acquires the mm lockfile so an autopull running in parallel can't
    race with our rename/unlink operations on the synced files.

    Backwards-compat letters: `c` / `f` from pre-v0.9.0 are still
    rejected loudly (real silent-data-loss risk pre-inversion). `b` /
    `both` from pre-v0.11.x is aliased to (s)kip with a one-time notice
    until 1.0 -- same on-disk effect, no risk in mapping it through.
    """
    config = _get_config()
    backend = get_backend(config)

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    failed = 0
    try:
        # Lock-protected discovery: opt into pre-inversion migration so any
        # legacy `.sync-conflict-<ts>-<dev>.<ext>` files get renamed to the
        # `v0-` prefix before resolve dispatches on the prefix below.
        hits = resolveflow._find_conflict_files(config, migrate_pre_inversion=True)

        if path:
            target = Path(path).expanduser().resolve()
            hits = [h for h in hits if h[1] == target]
            if not hits:
                _error(f"No conflict file matching: {path}")

        if not hits:
            console.print("[green]No conflict files.[/green]")
            return
        # Cache the device list ONCE -- N-conflict walks would otherwise hit
        # storage N times to attribute the REMOTE side, and iCloud cold-cache
        # reads can spike to multi-second per call.
        devices = list_devices(backend)
        sources_by_name = {s["name"]: s for s in get_sources(config)}
        _, failed = resolveflow._resolve_interactive_loop(hits, devices, sources_by_name)
    finally:
        release_lock()

    if failed:
        # Surface partial-failure as a non-zero exit so CI / scripts driving
        # `mm resolve` can detect that some conflicts were not actually
        # resolved (rename/unlink/read errors mid-walk). Walk continues
        # through every conflict; only the exit code reflects the failure.
        raise typer.Exit(1)


# ── auto commands (hook-safe: silent, never-prompt, typed errors) ─────


_AUTO_LOG_MAX_BYTES = 1_000_000
_AUTO_LOG_KEEP_BYTES = 512_000


_BREADCRUMB_STALE_AFTER_HOURS = 48


def _breadcrumb_staleness_suffix(ts: object) -> str:
    """Return a ``[yellow]stale[/yellow]`` marker when the breadcrumb is old.

    ``mm status`` renders the last autorun breadcrumb with no age check, so a
    device whose `mm autopull` / `mm autopush` stopped running entirely reports
    its last ``success`` indefinitely. Every other degradation signal mm has is
    written BY the command; this is the one case where nothing runs to write
    anything, which is exactly what a module-scope ``ImportError`` looks like.

    Best-effort: an unparseable or missing timestamp yields no marker rather
    than an error — the breadcrumb is diagnostics, not a correctness gate.
    """
    if not isinstance(ts, str):
        return ""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - when).total_seconds() / 3600.0
    if age_h < _BREADCRUMB_STALE_AFTER_HOURS:
        return ""
    return f" [yellow]stale — no autorun in {int(age_h)}h[/yellow]"


def _autorun_breadcrumb_path() -> Path:
    """`last-autorun.json` next to the recovery sidecar.

    Resolves `sidecar.SIDECAR_DIR` at call time so tests that monkeypatch
    the source constant get full isolation.
    """
    return sidecar.SIDECAR_DIR / "last-autorun.json"


def _migration_state_path() -> Path:
    """`migration-state.json` next to the recovery sidecar.

    Records that an auto-command observed missing recommended excludes but
    refused to mutate config (visible-failure contract — auto-commands MUST
    NEVER silently change user config). `mm status` and the interactive
    prompts read this so the signal stays visible until the user runs
    `mm migrate-config`.
    """
    return sidecar.SIDECAR_DIR / "migration-state.json"


def _write_migration_breadcrumb(missing: list[str]) -> None:
    """Best-effort write of the missing-excludes signal. Never raises.

    Skipped when no sources are missing (delete the file so stale
    breadcrumbs don't outlive their cause). Called from autopull/autopush
    on every run; cost is one stat + (rarely) one write.
    """
    path = _migration_state_path()
    if not missing:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            return
        return
    try:
        sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        import json as _json

        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "missing_sources": sorted(set(missing)),
        }
        path.write_text(_json.dumps(payload, indent=2))
    except Exception:
        # Forensic aid only; never block the calling auto-command.
        pass


def _maybe_prompt_migration(config: dict) -> None:
    """Once-per-invocation interactive prompt for pending config migrations.

    Called from the top of `mm push` / `mm pull` / `mm recapture` ONLY
    (interactive verbs). Auto-commands (autopull/autopush) NEVER prompt and
    NEVER mutate config — they write a `migration-state.json` breadcrumb
    instead and let `mm status` surface the signal. Visible-failure
    contract: silent config mutation in a hook would be exactly the class
    of "wedged sync I never noticed" failure the contract exists to prevent.
    """
    missing = _config_missing_recommended_excludes(config)
    retire_opencode = _explicit_opencode_source_present(config.get("sync", {}).get("sources"))
    if not missing and not retire_opencode:
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        # Non-TTY interactive verb (CI, piped invocation): warn to stderr
        # but don't block on a prompt nobody can answer.
        bits: list[str] = []
        if missing:
            bits.append(f"config missing recommended excludes for source(s) {', '.join(missing)}")
        if retire_opencode:
            bits.append("config still has a retired [[sync.sources]] opencode block")
        stderr_console.print(
            f"[yellow]warning:[/yellow] {'; '.join(bits)}. Run "
            f"[bold]mm migrate-config[/bold] to update."
        )
        return
    if missing:
        console.print(
            f"\n[yellow]Config is missing recommended exclude_patterns for "
            f"source(s):[/yellow] {', '.join(missing)}"
        )
        console.print(
            "  These per-machine artifacts (e.g. gstack repo-mode caches) "
            "produce churn on every pull when synced."
        )
    if retire_opencode:
        console.print(
            "\n[yellow]The opencode sync source was retired in v0.12.55.[/yellow] "
            "This config still has a leftover [[sync.sources]] opencode block."
        )
        console.print(
            "  mm migrate-config will remove it and record the name in "
            "disabled_sources so the next push does not mint deletion tombstones."
        )
    if typer.confirm("Run 'mm migrate-config' now?", default=True):
        # Call the core directly so typer's option machinery isn't in the
        # way; the inner "Apply these updates?" prompt still confirms
        # before writing.
        _migrate_config_core(yes=False, dry_run=False)


_AUTORUN_VERBS = ("push", "pull")


def _normalize_autorun_breadcrumbs(payload: object) -> dict[str, dict]:
    """Return the valid per-verb entries from current or legacy payload."""
    if not isinstance(payload, dict):
        return {}
    keyed: dict[str, dict] = {}
    for verb in _AUTORUN_VERBS:
        entry = payload.get(verb)
        if isinstance(entry, dict) and "outcome" in entry:
            keyed[verb] = entry
    if keyed:
        return keyed
    verb = payload.get("verb")
    if verb in _AUTORUN_VERBS and "outcome" in payload:
        entry = {k: v for k, v in payload.items() if k != "verb"}
        return {verb: entry}
    return {}


def _read_autorun_breadcrumbs() -> dict[str, dict]:
    """Load last-autorun.json keyed per verb under a read-only lock.

    Pre-0.12.45 files are a single `{verb, outcome, timestamp, detail?}`
    object; last-write-wins meant autopull erased a degraded push crumb.
    The new shape is `{"push": {...}, "pull": {...}}`. Legacy files are
    read as a one-verb map and rewritten on the next write.
    """
    try:
        with locked_json_snapshot(_autorun_breadcrumb_path()) as snapshot:
            if snapshot.data is None:
                return {}
            return _normalize_autorun_breadcrumbs(snapshot.data)
    except Exception:
        return {}


def _write_autorun_breadcrumb(verb: str, outcome: str, detail: str = "") -> None:
    """Record the last autopull/autopush attempt for forensic observability.

    Written on EVERY invocation -- success, lock-skip, config-missing,
    typed error, unexpected error. `mm status` surfaces this so a user can
    see 'last auto-sync attempt: 3h ago, skipped (lock held)' instead of
    wondering why sync appears wedged.

    Keyed per verb so the documented CLAUDE.md lifecycle (autopull at
    conversation start, autopush at end) cannot erase the other verb's
    crumb. The shared JSON lock serializes concurrent sibling hooks, so a
    lock-held pull cannot replace a degraded push read from a stale payload.
    Silent contract preserved: nothing is printed. Any failure here is
    swallowed -- a broken breadcrumb must never crash the hook.
    """
    entry: dict[str, str] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
    }
    if detail:
        entry["detail"] = detail
    try:
        with locked_json_rmw(_autorun_breadcrumb_path()) as ljson:
            if not ljson.is_locked:
                return
            existing = _normalize_autorun_breadcrumbs(ljson.data)
            existing[verb] = entry
            ljson.data.clear()
            ljson.data.update({k: existing[k] for k in _AUTORUN_VERBS if k in existing})
    except Exception:
        pass


def _auto_log_path(verb: str) -> Path:
    """`auto{verb}.log` next to the recovery sidecar.

    Resolves `sidecar.SIDECAR_DIR` at call time (not import time) so a test
    monkeypatching `mind_meld.sidecar.SIDECAR_DIR` alone is enough to redirect
    the log location. Import-time capture would be a foot-gun — a test that
    forgets to patch both the source constant and this module's alias would
    silently leak log writes to the user's real `~/.config/mind-meld/`.
    """
    return sidecar.SIDECAR_DIR / f"auto{verb}.log"


def _log_unexpected(verb: str, exc: BaseException) -> None:
    """Append a traceback block to ~/.config/mind-meld/auto{verb}.log.

    Hand-rolled (not `logging.handlers.RotatingFileHandler`) because this runs
    from short-lived typer commands: a named logger would either stack handlers
    across in-process test calls, or need explicit teardown.

    When the file exceeds `_AUTO_LOG_MAX_BYTES`, keep only the last
    `_AUTO_LOG_KEEP_BYTES` so the freshest tracebacks survive. The whole
    truncate/append sequence is guarded by `fcntl.flock(LOCK_EX)` so two
    racing failed hooks can't corrupt the log -- the original concern was
    that process A's truncate followed by process B's stat-read-truncate
    could silently erase A's just-written traceback, killing the one forensic
    artifact at the exact moment we needed it.

    Cause-chain logging (see docstring in _should_log_cause): if `exc.__cause__`
    is set (typed-error raised via `raise X from e`), we include the full
    chain so a `ConfigError` wrapping a `PermissionError` doesn't lose the
    original OSError + errno. For typed errors with no cause, this function
    is not called -- the caller short-circuits to stderr-only.

    Any failure here is swallowed; the caller has already emitted the
    one-line stderr message, and a broken log file must never crash the hook.
    """
    try:
        sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        path = _auto_log_path(verb)
        block = (
            f"--- {datetime.now(timezone.utc).isoformat()} mm {__version__} "
            f"auto{verb}\n"
            f"{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}"
            f"\n"
        )
        # Open read-write, create if missing. One fd, one lock, full rotate+append
        # in a single critical section. "ab+" would truncate to end on open on
        # some platforms; "a+b" is portable and gives us seek freedom.
        with open(path, "a+b") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0, 2)  # end
                size = f.tell()
                if size > _AUTO_LOG_MAX_BYTES:
                    f.seek(size - _AUTO_LOG_KEEP_BYTES)
                    tail = f.read()
                    f.seek(0)
                    f.truncate()
                    f.write(tail)
                f.write(block.encode("utf-8"))
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        # Logging a traceback must never mask the real error.
        pass


@dataclass
class _AutoSetup:
    config: dict
    passphrase: str
    memory_kb: int


def _should_log_cause(exc: BaseException) -> bool:
    """Log a typed error iff it wraps a non-typed cause.

    Pure validation errors like `ConfigError("missing [device] section")` are
    raised without `from`, so `__cause__` is None and the stderr one-liner
    already contains everything a user needs. Adversarial/environmental
    errors wrap an underlying exception via `raise X from e` -- e.g.
    `ConfigError("failed to parse config.toml - ...") from tomllib.TOMLDecodeError`
    or a future `ConfigError from PermissionError`. In those cases the
    stderr message loses the original traceback/errno, so the log is the
    only place the original cause survives.
    """
    return exc.__cause__ is not None


def _print_auto_typed_error(verb: str, verb_action: str, exc: BaseException) -> None:
    """Render a typed hook error safely on its plain stderr surface."""
    print(
        f"mm: {verb} {verb_action} - {strip_terminal_escapes(str(exc))}",
        file=sys.stderr,
    )


def _auto_command_setup(verb: str) -> _AutoSetup | None:
    """Load config + passphrase + crypto session for autopull/autopush.

    Returns None when the caller should exit (silent or after a typed error).
    Logging policy:
      - Typed error with no __cause__ (pure validation): stderr only, no log.
        Expected conditions (missing passphrase, missing field) don't need
        a traceback to diagnose.
      - Typed error WITH __cause__ (wrapped OSError / TOMLDecodeError / etc.):
        stderr + log. The wrapper message drops the underlying traceback +
        errno, so the log is the only forensic artifact.
      - Unexpected (non-MindMeldError) exception: always stderr + log.

    Semantics:
      - config missing          -> silent return (not initialized)
      - config typed no-cause   -> typed one-line stderr (no log)
      - config typed w/cause    -> typed one-line stderr + full chain to log
      - config unexpected error -> one-line stderr + traceback to log
      - keyring + env empty     -> typed one-line stderr (no log)
      - crypto typed no-cause   -> typed one-line stderr (no log)
      - crypto typed w/cause    -> typed one-line stderr + full chain to log
      - crypto unexpected error -> one-line stderr + traceback to log

    Does NOT acquire the lockfile; caller owns that and decides lock-policy.

    Writes a breadcrumb (`last-autorun.json`) on every early-exit path so
    `mm status` can surface silent-skip history.
    """
    # All CONFIG_PATH access goes through `_config_module.CONFIG_PATH` so
    # `monkeypatch.setattr("mind_meld.config.CONFIG_PATH", ...)` propagates.
    # `load_config()` already resolves through the module attribute; a
    # local `from mind_meld.config import CONFIG_PATH` binding would let
    # the two diverge under test (the silent-mode contract regression
    # that surfaced in v0.8.15).
    if not _config_module.CONFIG_PATH.exists():
        _write_autorun_breadcrumb(verb, "config-missing")
        return None

    try:
        config = load_config()
    except MindMeldError as e:
        _print_auto_typed_error(verb, "failed", e)
        if _should_log_cause(e):
            _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "config-error", str(e))
        return None
    except Exception as e:
        print(
            f"mm: {verb} failed - unexpected config error (see auto{verb}.log)",
            file=sys.stderr,
        )
        _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "config-error", type(e).__name__)
        return None

    # Seam 1 — transition detection on the silent autopull/autopush path.
    # Same hook as `_get_config`, but routed here directly to preserve this
    # function's silent-on-error contract (we MUST NOT shout on Claude Code
    # session start when config is broken or missing).
    upgrade.run_transition_hook(config)

    try:
        passphrase = get_passphrase(non_interactive=True)
    except CryptoError as e:
        _print_auto_typed_error(verb, "skipped", e)
        _write_autorun_breadcrumb(verb, "no-passphrase")
        return None
    except Exception as e:
        # crypto.get_passphrase narrowed its keyring catch to
        # (KeyringError, ImportError) in v0.8.9. Other exception kinds
        # (OSError from a locked keychain, RuntimeError from a broken
        # Linux DBus backend) now propagate here. Without this guard they
        # would escape the hook entirely — no breadcrumb, no stderr line,
        # silent sync stop. Mirrors the backend-error shape below.
        print(
            f"mm: {verb} failed - keyring error (see auto{verb}.log)",
            file=sys.stderr,
        )
        _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "keyring-error", type(e).__name__)
        return None

    # get_backend() can still raise on malformed config["storage"]["path"]
    # (load_config validates presence, not types). Guard it the same way
    # as the crypto-session call so the hook contract holds end-to-end.
    try:
        backend = get_backend(config)
    except Exception as e:
        print(
            f"mm: {verb} failed - backend init failed (see auto{verb}.log)",
            file=sys.stderr,
        )
        _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "backend-error", type(e).__name__)
        return None

    try:
        memory_kb = _init_crypto_session(backend, passphrase, config)
    except MindMeldError as e:
        _print_auto_typed_error(verb, "failed", e)
        if _should_log_cause(e):
            _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "crypto-error", str(e))
        return None
    except Exception as e:
        print(
            f"mm: {verb} failed - unexpected crypto error (see auto{verb}.log)",
            file=sys.stderr,
        )
        _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "crypto-error", type(e).__name__)
        return None

    return _AutoSetup(config=config, passphrase=passphrase, memory_kb=memory_kb)


@contextmanager
def _auto_command_scope(
    verb: str,
    *,
    typer_exit_outcome: str,
) -> Iterator[_AutoSetup | None]:
    """Own the shared unattended-command control flow.

    Pull and push keep their own tail bodies and success/degradation outcome
    mappings. This scope owns only the shared setup, migration breadcrumb,
    lock lifecycle, and common exception-to-breadcrumb policy.
    """
    setup = _auto_command_setup(verb)
    if setup is None:
        yield None
        return

    _write_migration_breadcrumb(_config_missing_recommended_excludes(setup.config))

    try:
        acquire_lock()
    except LockError:
        _write_autorun_breadcrumb(verb, "lock-held")
        yield None
        return

    try:
        yield setup
    except typer.Exit:
        _write_autorun_breadcrumb(verb, typer_exit_outcome)
    except MindMeldError as e:
        _print_auto_typed_error(verb, "failed", e)
        if _should_log_cause(e):
            _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "failed", str(e))
    except Exception as e:
        print(
            f"mm: {verb} failed - unexpected error (see auto{verb}.log)",
            file=sys.stderr,
        )
        _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "failed", type(e).__name__)
    finally:
        release_lock()


@app.command()
def autopull() -> None:
    """Pull changes silently. Designed for Claude Code -- no prompts, minimal output.

    Never prompts (`get_passphrase(non_interactive=True)`). Silent exit on:
    missing config, missing passphrase, lock contention. Loud exit (one-line
    stderr + traceback to `~/.config/mind-meld/autopull.log`) on: corrupt
    config, crypto init failure, unexpected bug inside `_pull_core`.
    """
    with _auto_command_scope("pull", typer_exit_outcome="fleet-refused") as setup:
        if setup is None:
            return

        result = _pull_core(
            setup.config,
            setup.passphrase,
            setup.memory_kb,
            quiet=True,
            conflict_mode="keep-both",
        )

        if result.total_applied:
            parts = []
            if result.total_written:
                parts.append(f"{result.total_written} written")
            if result.total_merged:
                parts.append(f"{result.total_merged} merged")
            if result.total_conflicted:
                parts.append(f"{result.total_conflicted} conflicts")
            src_display = ", ".join(result.device_names)
            total = result.total_applied
            print(f"mm: pulled {total} files from {src_display} ({', '.join(parts)})")
            if result.total_conflicted:
                print(
                    f"mm: {result.total_conflicted} conflicts - run 'mm conflicts' to review",
                    file=sys.stderr,
                )
        if result.total_skipped_unknown_source:
            print(
                f"mm: skipped {result.total_skipped_unknown_source} unknown "
                "source(s) - run 'mm sources' to reconcile config",
                file=sys.stderr,
            )
        if result.total_failed:
            # Per-file failures (decrypt error, conflict rename failure, write
            # failure, ValueError on corrupted device_id) increment total_failed
            # in _apply_incoming_file. Without this stderr surface, autopull's
            # quiet contract silently swallows the summary too — exactly the
            # silent-failure pattern Track 1A's helper-level audit was meant
            # to close.
            print(
                f"mm: {result.total_failed} file(s) failed - "
                "run 'mm pull --verbose' to see details",
                file=sys.stderr,
            )

        # Degraded outcome: pull succeeded overall but some part of it is
        # at-risk. Mirrors the v0.8.1 `no-sources` autopush breadcrumb
        # pattern — without a distinct outcome, `mm status` shows the last
        # run as "success" indefinitely while data may not survive crash
        # (fsync) or may be partition-drifting (corrupt peers, unknown
        # sources, per-file failures). Stderr warnings for the same signals
        # already fire in _print_pull_summary (corrupt_peers/fsync_warnings)
        # and in the total_skipped_unknown_source / total_failed prints
        # earlier in this function. The breadcrumb makes the degradation
        # state persistent for monitoring on top of `mm status` / `mm diag`.
        degradations: list[str] = []
        if result.durability_fsync_failures:
            degradations.append(f"fsync failed on {result.durability_fsync_failures} parent dir(s)")
        if result.corrupt_peer_count:
            degradations.append(f"{result.corrupt_peer_count} corrupt peer manifest(s)")
        if result.total_skipped_unknown_source:
            degradations.append(f"{result.total_skipped_unknown_source} unknown source(s)")
        if result.total_failed:
            degradations.append(f"{result.total_failed} file(s) failed")

        if degradations:
            _write_autorun_breadcrumb("pull", "degraded", "; ".join(degradations))
        else:
            _write_autorun_breadcrumb("pull", "success")

        # Seam 2 — auto-upgrade nudge emission at the TAIL. Runs AFTER the
        # main work + breadcrumb so the cold-cache HTTP fetch latency
        # (~500ms 1x/24h) doesn't stack on sync latency. Silent unless an
        # upgrade is genuinely available AND the 24h re-nudge gate permits.
        upgrade.emit_nudge_if_due(setup.config)


@app.command()
def autopush() -> None:
    """Push changes silently. Designed for Claude Code -- no prompts, minimal output.

    Never prompts (`get_passphrase(non_interactive=True)`). Silent exit on:
    missing config, missing passphrase, lock contention. Loud exit (one-line
    stderr + traceback to `~/.config/mind-meld/autopush.log`) on: corrupt
    config, crypto init failure, unexpected bug inside `_push_core`. No
    auto-GC on autopush (prevents blob-deletion hole).
    """
    with _auto_command_scope("push", typer_exit_outcome="refused") as setup:
        if setup is None:
            return

        result = _push_core(
            setup.config,
            setup.passphrase,
            setup.memory_kb,
            quiet=True,
        )

        if result is None and not get_sources(setup.config):
            # Distinguish "broken config no-op" from "nothing to push" no-op
            # in the breadcrumb. _push_core already printed the stderr warning;
            # if `mm status` only sees "success" forever, monitoring never
            # catches the wedge.
            _write_autorun_breadcrumb("push", "no-sources")
            return

        if result:
            parts = []
            if result.total_new:
                parts.append(f"{result.total_new} new")
            if result.total_modified:
                parts.append(f"{result.total_modified} modified")
            if result.total_deleted:
                parts.append(f"{result.total_deleted} deleted")
            total = result.total_new + result.total_modified + result.total_deleted
            print(f"mm: pushed {total} files ({', '.join(parts)})")

        # Persist events-tail degradation the same way autopull persists its
        # own (see the `degradations` list in `autopull` below). The tail is
        # forensic-only and swallows its failures behind a `mm: notice:` line,
        # but autopush runs unattended from a Claude Code hook — that stderr
        # reaches nobody, so pre-v0.12.16 the breadcrumb said `success` while
        # the retro pipeline was dead, and `mm status` repeated it. Same
        # argument CLAUDE.md already makes for the `no-sources` breadcrumb:
        # without this, `mm status` only ever sees `success` and monitoring
        # built on top of it never catches the wedge.
        events_degradations = result.events_degradations if result else []
        if events_degradations:
            _write_autorun_breadcrumb("push", "degraded", "; ".join(events_degradations))
        else:
            _write_autorun_breadcrumb("push", "success")

        # Seam 2 — auto-upgrade nudge emission at the TAIL (mirrors autopull).
        upgrade.emit_nudge_if_due(setup.config)


# ── helpers ───────────────────────────────────────────────────────────


def _default_device_name() -> str:
    """Generate a default device name from hostname."""
    return socket.gethostname()


if __name__ == "__main__":
    app()
