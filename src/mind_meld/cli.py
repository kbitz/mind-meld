"""Mind Meld CLI — built with Typer.

Commands: init, push, pull, status, devices, diff, gc, autopull, autopush,
          sources, conflicts, resolve.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import os
import re
import secrets
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import typer
from rich.console import Console
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
    fsutil,
    identity,
    pullhistory,
    seen_sources,
    sidecar,
    token_usage,
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
    load_config,
    patch_config_on_disk,
    save_config,
)
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
from mind_meld.lockedjson import locked_json_rmw
from mind_meld.lockfile import acquire_lock, release_lock
from mind_meld.manifest import (
    CONFLICT_INFIX,
    DiffResult,
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
    mtime_from_manifest,
    mtime_from_path,
    parse_conflict_device_short,
    read_and_hash,
    serialize_manifest,
)
from mind_meld.merge import merge_file, should_merge
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
FetchStatus = Literal["ok", "missing", "corrupt"]
CONFLICT_AGE_DAYS = 30
# Track 7B (v0.10.3): per-device daily JSONL events files older than this
# are reaped at every `mm gc`. The retention is fleet policy, not per-
# device opt-in: a stale device's old events would otherwise pin storage
# forever via tombstone propagation. Reap by FILENAME date (Codex C5,
# C6) — iCloud restores produce misleading mtimes.
EVENTS_RETENTION_DAYS = 90
_EVENTS_FILENAME_DATE_RE = re.compile(r"^(?P<device>.+)-(?P<date>\d{4}-\d{2}-\d{2})\.jsonl$")

# Group 8 / Track 8A: 24h-TTL gate for the retro-fleet skill symlink installer.
# Two markers (cross-model #3 from /plan-eng-review): success caches the happy
# path; conflict-skip suppresses the per-push notice when the user has their
# own file at the target. Transient failure paths (OSError) leave both
# untouched so next push retries — matches the visible-failure contract.
SKILL_LINK_TTL_SECONDS = 24 * 60 * 60
_SKILL_LINK_NAME = "retro-fleet"
_SKILL_LINK_SUCCESS_MARKER = "skill-link-checked"
_SKILL_LINK_CONFLICT_MARKER = "skill-link-conflict"

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


app = typer.Typer(
    name="mm",
    help="Mind Meld — sync Claude Code sessions and other sources across machines.",
    add_completion=False,
)
console = Console()
# Dedicated stderr sink for _error and other failure-path output. Rich
# formatting is preserved in interactive terminals; pipes (autopush/autopull
# quiet mode, CI) get a clean stdout and one-line stderr per the contract
# documented in README.md "Claude Code Integration." Using a single module-
# level instance rather than constructing ad-hoc keeps color-capability
# detection and terminal-width behavior consistent across call sites.
stderr_console = Console(stderr=True)


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
    stderr_console.print(f"[red]Error:[/red] {msg}")
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
        stderr_console.print(f"[yellow]Warning:[/yellow] dropped device entry {key} — {reason}")

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


def _build_exclude_map(config: dict) -> dict[str, list[str]]:
    """Map source name -> exclude_patterns list from local config.

    Empty entries are pruned so callers can use truthiness as the no-op gate.
    Sources without `exclude_patterns` are absent (not present-with-[]) so
    `_filter_excluded_paths` short-circuits via the `not exclude_map` check.
    """
    out: dict[str, list[str]] = {}
    for src in get_sources(config):
        patterns = src.get("exclude_patterns")
        if patterns:
            out[src["name"]] = list(patterns)
    return out


def _filter_excluded_paths(manifest: dict, exclude_map: dict[str, list[str]]) -> dict:
    """Return a shallow copy of `manifest` with excluded source-paths stripped.

    Drops entries from `sources.<name>.files` and `tombstones` whose relative
    path matches any glob in `exclude_map[name]`. Empty `exclude_map` returns
    `manifest` unchanged (load-bearing for hot paths: avoid copying large
    manifests when no source declares excludes).

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
    if not exclude_map:
        return manifest

    def _excluded(src_name: str, rel_path: str) -> bool:
        patterns = exclude_map.get(src_name)
        if not patterns:
            return False
        return any(fnmatch.fnmatch(rel_path, p) for p in patterns)

    out = dict(manifest)

    new_sources: dict[str, dict] = {}
    for src_name, src_data in manifest.get("sources", {}).items():
        patterns = exclude_map.get(src_name)
        if not patterns:
            new_sources[src_name] = src_data
            continue
        new_files = {
            rel: info
            for rel, info in src_data.get("files", {}).items()
            if not _excluded(src_name, rel)
        }
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
    via get_sources's filter). Peers pull tombstones, delete content
    fleet-wide. P0 footgun.

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
    import hashlib
    import json

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
    """Compute the sibling path used to preserve a local divergent version.

    Syncthing convention: <stem>.sync-conflict-<YYYYMMDD-HHMMSS>-<device_short>.<ext>

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
    base_name = f"{stem}{CONFLICT_INFIX}{ts}-{device_short}"
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
) -> tuple[str, bytes | None]:
    """Prompt interactively for how to handle one conflict. Default skip.

    ``peer_name`` is the human-readable name of the device that pushed the
    remote bytes (resolved by the caller via ``lookup_device_by_short_id``);
    ``None`` if unknown or ambiguous. ``ambiguous_count`` is the number of
    matching peers when the device-id prefix collides (>=2); zero otherwise.
    Both flow into the REMOTE banner so the user sees attribution at the
    moment of the choice.

    Returns ``(choice, merged_bytes)``. Choice is one of:
    ``keep-canonical`` (= keep local), ``keep-remote``, ``merge``,
    ``abort``, ``keep-both``. ``keep-both`` is what (s)kip emits today --
    the on-disk effect is "leave both files in place." ``merge`` is
    accompanied by ``merged_bytes`` -- the LCS-merged result for the
    caller to write to ``local_path``. For all other choices
    ``merged_bytes`` is ``None``.
    """
    import difflib

    from mind_meld.conflictdiff import (
        count_divergent_lines,
        render_banner,
        render_prompt,
    )
    from mind_meld.merge import lcs_merge

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
    merge_available = merge_conflicts >= 0

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
    console.print(
        render_banner(
            "remote",
            local_path.name,
            peer_name,
            ambiguous_count=ambiguous_count,
        )
    )

    # Inline pull-time site is post_inversion only (see comment above):
    # diff is local -> remote, so m = local-only, n = remote-only directly.
    m, n, k = count_divergent_lines(diff)
    if k:
        console.print(
            f"  [dim]{m} unique line{'' if m == 1 else 's'} of yours; "
            f"{n} unique line{'' if n == 1 else 's'} from peer; "
            f"{k} total diff lines.[/dim]"
        )

    if diff:
        for line in diff[:60]:
            # Diff lines carry peer-controlled bytes (file contents).
            # Use safe_text() so Rich strips terminal escapes (CSI/OSC/DCS)
            # AND defangs markup -- Text() alone passes raw escapes through.
            if line.startswith("+") and not line.startswith("+++"):
                console.print(safe_text(line, style="green"))
            elif line.startswith("-") and not line.startswith("---"):
                console.print(safe_text(line, style="red"))
            else:
                console.print(safe_text(line))
        if len(diff) > 60:
            console.print(f"  [dim]...({len(diff) - 60} more diff lines)[/dim]")
    else:
        console.print("  [dim](files differ but text diff is empty \u2014 likely binary)[/dim]")

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
    prompt_default = "m" if (merge_available and merge_conflicts == 0) else "s"
    choice = typer.prompt("Choice", default=prompt_default, show_default=False).strip().lower()

    # Pre-1.0 deprecation alias: `b` / `both` used to mean "keep both"
    # which is exactly the on-disk effect of (s)kip today. Map through
    # with a one-time notice so users learn the new letter.
    if choice in ("b", "both"):
        print(
            "mm: notice: 'b' / 'both' now means 'skip'; use 's' going forward "
            "(alias removed at 1.0).",
            file=sys.stderr,
        )
        choice = "s"

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


def _apply_write(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    verbose: bool = False,
) -> ApplyOutcome:
    """[W] local has no copy \u2014 atomic_write remote to canonical."""
    try:
        # Deferred durability: per-file fsync=False; end of pull calls
        # fsutil.fsync_dir once per touched parent.
        fsutil.atomic_write_bytes(local_path, plain_data, fsync=False)
    except (OSError, StorageError) as e:
        console.print(f"  [red]write failed:[/red] {safe_str(rel_path)} \u2014 {safe_str(e)}")
        return "failed"
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
    # filename has no `v0-` prefix \u2014 that prefix is reserved for
    # pre-inversion files migrated by `_migrate_pre_inversion_conflict`,
    # NOT new files produced post-inversion.
    try:
        fsutil.atomic_write_bytes(conflict_path, plain_data, fsync=False)
    except (OSError, StorageError) as e:
        console.print(
            f"  [red]sidecar write failed (local preserved):[/red] "
            f"{safe_str(rel_path)} \u2014 {safe_str(e)}"
        )
        return "failed"

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
) -> ApplyOutcome:
    """Dispatch one decrypted remote file to the appropriate _apply_* helper.

    See the decision-tree comment above for branch semantics. The local file
    is never destroyed without a recoverable trail (either conflict copy
    or rollback).
    """
    local_path.parent.mkdir(parents=True, exist_ok=True)

    if not local_path.exists():
        return _apply_write(local_path, rel_path, plain_data, verbose=verbose)

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
    remote_mtime_str = remote_info.get("mtime")
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
            rel_path, local_path, plain_data, peer_name, ambiguous_count
        )
        if choice == "keep-canonical":
            # Post-inversion: canonical IS local, so "keep-canonical" =
            # "keep-local" — both work as user-facing labels. The internal
            # outcome stays "skipped" for back-compat with PullResult.
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
        # choice == "keep-both" -> fall through to _apply_conflict

    return _apply_conflict(local_path, rel_path, plain_data, remote_device_id, verbose=verbose)


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
) -> tuple[int, dict[ApplyOutcome, list[str]]]:
    """Download blobs and dispatch each to _apply_incoming_file.

    Returns (encrypted_bytes_transferred, outcomes_by_path).
    outcomes_by_path groups rel_paths by outcome so callers can report
    per-outcome totals and write accurate sync logs.

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

            outcome = _apply_incoming_file(
                local_path=local_path,
                rel_path=rel_path,
                plain_data=plain_data,
                remote_info=info,
                remote_device_id=source_device_id,
                interactive_resolve=interactive_resolve,
                verbose=verbose and not quiet,
                devices=devices,
            )
            outcomes[outcome].append(rel_path)
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


def _prompt_source_toggle(source: dict[str, Any], *, current_state: bool) -> bool:
    """One Y/n confirm for a single source.

    Single source of truth for the prompt copy + default-Y/N rule. Used
    by `_prompt_sources` (init flow) and `reconfigure_sources` (eng-review
    D5). `current_state` is the default answer:
      - init: whether the path exists on disk (kept-as-is per eng-review D1)
      - reconfigure: whether the source is currently active in config
    """
    name = source["name"]
    path_str = str(source.get("path", ""))
    if path_str:
        detected = "detected" if Path(path_str).expanduser().exists() else "not detected"
        prompt = f"Sync '{name}' source at {path_str}? ({detected})"
    else:
        prompt = f"Sync '{name}' source?"
    return typer.confirm(prompt, default=current_state)


def _prompt_sources() -> list[dict[str, Any]]:
    """Prompt for each known source type; return the enabled entries.

    User-facing sources (claude, gstack) become a Y/n prompt via
    `_prompt_source_toggle`. Default is Y when the path exists on disk,
    N otherwise — nudges users toward only-enabling-what-they-have
    without making it impossible to enable a source whose directory
    doesn't exist yet (e.g. new machine, same project about to be cloned).

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
        path_str = str(default["path"])
        exists = Path(path_str).expanduser().exists()
        if _prompt_source_toggle(default, current_state=exists):
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
            root_salt = retry_fetch.root_salt
            argon2_memory_kb = retry_fetch.argon2_memory_kb
            keycheck_blob = retry_fetch.keycheck_blob
            set_crypto_session(root_salt, argon2_memory_kb)
            master_key = load_master_key(passphrase, root_salt, argon2_memory_kb)
            try:
                verify_passphrase(master_key, keycheck_blob)
            except CryptoError as e:
                _error(str(e))
            console.print("  Verified passphrase against peer mm-crypto-init.")
            return root_salt, argon2_memory_kb, keycheck_blob

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
    console.print(
        f"  Verified passphrase against existing mm-crypto-init "
        f"(root_salt fp={root_salt_fingerprint(root_salt)})."
    )
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
    _ensure_retro_skill_link(dry_run=False)

    # Init-time event backfill (v0.11.8). Captures the past 30 days of git
    # commits + a full sessions inventory so retro-fleet works immediately
    # after init, without waiting for the first push to populate events.
    # Resolves sources via get_sources() so mm-events bootstraps the events
    # dir before walk runs. Forensic-only on failure; init proceeds.
    resolved_sources = get_sources(config)
    _run_events_backfill(config, resolved_sources, device_id)

    console.print("\n[green]Mind Meld initialized. Run 'mm push' to sync.[/green]")


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
                gc_count = _do_gc(config, passphrase, memory_kb, dry_run=False, verbose=False)
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


def _ensure_retro_skill_link(*, dry_run: bool = False) -> None:
    """Group 8 / Track 8A symlink self-heal for the retro-fleet skill.

    Three states (cross-model #3 from /plan-eng-review uses a 2-marker gate
    so deliberate-conflict skips don't spam stderr forever):

    * **success** — target absent OR target is a correct symlink at our
      skill source. Idempotent. Touch ``skill-link-checked`` marker.
    * **conflict-skip** — target exists as a real file or wrong symlink.
      Don't clobber the user's file. Emit ``mm: notice:`` once per 24h
      (gated by ``skill-link-conflict`` marker). User can ``rm`` to take
      mm's version.
    * **transient-failure** — TOCTOU FileExistsError, PermissionError on
      read-only ~/.claude, OSError on a filesystem without symlink
      support. CQ#1 forensic-only contract: emit ``mm: notice:``,
      return, leave both markers alone so next push retries.

    Dangling-symlink branch (Test review #1 IRON-RULE pin from
    /plan-eng-review): a symlink whose target was deleted (e.g., after
    ``pipx reinstall`` rebuilt the venv at a different path) is unlinked
    and replaced. Pre-fix, ``target.is_symlink() and target.resolve() ==
    src.resolve()`` skipped this case because resolve() returns the bad
    path; the second branch then matched ``target.is_symlink()`` and
    routed into "exists, don't replace" — silent permanent broken state.

    Called from ``mm init`` (always, no gate) and ``_push_core`` HEAD
    (24h-TTL gated). Both gates are read with ``os.stat`` wrapped in
    try/except (TODO#3 critical-gap fix: EACCES on the marker dir must
    fail-open so push doesn't crash).
    """
    if dry_run:
        return

    target = Path("~/.claude/skills").expanduser() / _SKILL_LINK_NAME
    skills_dir = target.parent
    if not skills_dir.exists():
        # Silent skip — no Claude Code installed on this machine. Touching
        # the success marker would suppress retries if the user installs
        # Claude Code later in the day; leave it alone so the 24h check
        # naturally re-evaluates after sync.
        return

    try:
        skill_src = _resolve_retro_skill_src()
    except Exception as e:
        sys.stderr.write(
            f"mm: notice: retro-fleet skill source unresolvable: "
            f"{type(e).__name__}: {safe_str(e)}\n"
        )
        return

    # Branch 1: dangling symlink → unlink + recreate.
    # Path.exists() returns False on a dangling symlink while is_symlink()
    # returns True. This branch was missing in the original /plan-eng-review
    # design and is REGRESSION-class for pipx-reinstall recovery.
    if target.is_symlink() and not target.exists():
        try:
            target.unlink()
        except OSError as e:
            sys.stderr.write(
                f"mm: notice: retro-fleet skill dangling-link cleanup failed: "
                f"{type(e).__name__}: {safe_str(e)}\n"
            )
            return
        # Fall through to symlink_to creation below.
    # Branch 2: target is a correct, intact symlink to our source → no-op.
    elif target.is_symlink() and target.exists():
        try:
            if target.resolve() == skill_src.resolve():
                _touch_marker(_SKILL_LINK_SUCCESS_MARKER)
                return
        except OSError:
            # resolve() can raise on a path with permission issues — fall
            # through to the conflict-skip branch.
            pass
        # Wrong target — user's own symlink elsewhere. Conflict-skip.
        _emit_conflict_notice(target)
        return
    # Branch 3: a real file or directory at the target → conflict-skip.
    elif target.exists():
        _emit_conflict_notice(target)
        return

    # Branch 4: target is absent (or just unlinked from dangling branch above).
    # Create the symlink.
    try:
        target.symlink_to(skill_src)
    except OSError as e:
        # CQ#1: TOCTOU FileExistsError, EACCES, EPERM, ENOTSUP — forensic
        # only. Don't crash push; don't touch markers; next push retries.
        sys.stderr.write(
            f"mm: notice: retro-fleet skill link install failed: "
            f"{type(e).__name__}: {safe_str(e)}\n"
        )
        return
    _touch_marker(_SKILL_LINK_SUCCESS_MARKER)


def _resolve_retro_skill_src() -> Path:
    """Return the on-disk dir that the symlink should point at.

    Subtle: the on-disk dir is named ``retro_fleet`` (Python identifier) but
    the symlink target name is ``retro-fleet`` (Claude Code skill convention).
    The aggregator imports cleanly via ``mind_meld.skills.retro_fleet`` and
    Claude Code reads the symlinked dir as ``retro-fleet``.
    """
    import importlib.resources

    return Path(str(importlib.resources.files("mind_meld") / "skills" / "retro_fleet"))


def _emit_conflict_notice(target: Path) -> None:
    """Notice once per 24h — gated by the conflict marker. Cross-model #3
    from /plan-eng-review: per-push spam on a deliberate conflict is
    hostile; the gate suppresses repeats."""
    if _marker_is_fresh(_SKILL_LINK_CONFLICT_MARKER):
        return
    sys.stderr.write(
        f"mm: notice: skill at {safe_str(str(target))} exists; not replacing "
        f"(remove the file to take mm's retro-fleet skill)\n"
    )
    _touch_marker(_SKILL_LINK_CONFLICT_MARKER)


def _marker_is_fresh(name: str) -> bool:
    """Return True iff the marker exists AND its mtime is within
    ``SKILL_LINK_TTL_SECONDS``. TODO#3 critical-gap fix: stat failure
    fail-open (treat as if no marker — re-run installer)."""
    marker = _config_dir() / f".{name}"
    try:
        st = marker.stat()
    except OSError:
        # FileNotFoundError, EACCES, EIO — fail-open. Returns False so the
        # caller runs the installer; matches the visible-failure contract
        # (no silent broken state).
        return False
    age = time.time() - st.st_mtime
    return age < SKILL_LINK_TTL_SECONDS


def _touch_marker(name: str) -> None:
    """Mtime-touch the named marker. Best-effort; OSError is swallowed
    silently (the next push will simply re-run the installer)."""
    marker_dir = _config_dir()
    try:
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / f".{name}").touch()
    except OSError:
        pass


def _config_dir() -> Path:
    return Path("~/.config/mind-meld").expanduser()


def _skill_link_check_due() -> bool:
    """Gate consulted by ``_push_core``. Returns True when the installer
    should run.

    Two paths to True:

    1. **Marker is stale** (or absent) — the original 24h-TTL behavior.
    2. **Marker is fresh but link state has drifted** — link is missing,
       dangling, or pointing somewhere other than our source. Pre-fix
       (post-v0.11.0 / pre-this-fix) the fresh marker silently suppressed
       self-heal for 24h. The case in the wild: pipx-installed mm 0.11.0
       creates the link successfully and touches the marker; user later
       removes the link manually (e.g. cleaning up an old conductor
       workspace whose path the link used to point at on a previous
       install); next push sees fresh marker + missing link and skips
       the installer for the rest of the day. The drift check costs one
       ``lstat`` + one ``readlink`` + ``importlib.resources`` resolution
       on the steady-state path — negligible vs the rest of push.

    Any I/O or resolver error in the drift check fails open (returns
    True) so the installer runs and emits its own notice. The conflict
    marker is consulted separately by ``_emit_conflict_notice``.
    """
    if not _marker_is_fresh(_SKILL_LINK_SUCCESS_MARKER):
        return True
    target = Path("~/.claude/skills").expanduser() / _SKILL_LINK_NAME
    try:
        if not target.is_symlink():
            return True
        if not target.exists():
            return True  # dangling
        skill_src = _resolve_retro_skill_src()
        if target.resolve() != skill_src.resolve():
            return True  # wrong target (e.g. stale workspace path)
    except Exception:
        return True
    return False


def _enabled_claude_paths(sources: list[dict]) -> list[Path]:
    """Return the base directory of each ``type=claude`` source resolved by
    ``get_sources()``. Used by ``_run_events_tail`` to feed Track 7B's
    ``walk_session_metadata`` once per claude dir; aggregated into a single
    sessions-snapshot row so pull-merge set-union semantics stay stable
    regardless of how many claude sources are configured."""
    return [Path(s["path"]).expanduser() for s in sources if s.get("type") == "claude"]


def _decide_token_walk_policy(
    claude_paths: list[Path],
    *,
    quiet: bool,
) -> bool:
    """Return True if the events tail should aggregate token data this push.

    Side effect: when cold cache + (interactive OR detected upgrade
    transition), runs ``warm_token_cache_inline`` to populate the cache
    BEFORE the tail walk starts. False return means cold cache + no warm
    eligibility (autopush, no transition) — emit a notice and skip.

    Four policies:

    1. **Cache already warm**: return True. Tail walk picks up cache hits
       on every existing jsonl, walks only newly-touched ones.

    2. **Cold + interactive (``quiet=False``)**: telegraph one-time warm
       cost, run ``warm_token_cache_inline``, return True.

    3. **Cold + autopush + transition fired**: warm silently using the
       ``warm_token_cache_inline`` default budget. The transition flag is
       set by the call to ``upgrade.run_transition_hook`` earlier in the
       same process — its presence (not a budget bump) is what unlocks
       this path on autopush.

    4. **Cold + autopush + no transition**: emit ``mm: notice: token cache
       not warm; run 'mm push' to populate`` and return False (skip token
       aggregation this push).
    """
    if not claude_paths:
        return False
    try:
        is_cold = token_usage.is_cache_cold()
    except OSError:
        return False
    if not is_cold:
        return True
    transition = upgrade.last_transition_seen()
    if quiet and transition is None:
        sys.stderr.write("mm: notice: token cache not warm; run 'mm push' to populate\n")
        return False
    if not quiet:
        sys.stderr.write("mm: warming token cache (one-time, ~3s)...\n")
    try:
        token_usage.warm_token_cache_inline(claude_paths)
    except Exception as e:
        sys.stderr.write(
            f"mm: notice: token cache warm failed: {type(e).__name__}: {safe_str(e)}\n"
        )
        return False
    return True


def _run_events_tail(
    config: dict,
    sources: list[dict],
    device_id: str,
    *,
    dry_run: bool,
    quiet: bool,
) -> None:
    """Capture per-push fleet-retro events at the HEAD of ``_push_core``.

    See CLAUDE.md "Events tail in _push_core (load-bearing, v0.10.3)" for
    the load-bearing invariants: head-position single-call-site (Codex C4
    — branch-fragility-free, one-push-lag-free), dry_run no-op (preview
    contract), mm-events-resolved gate (covers fresh / migrated / un-
    migrated configs uniformly, Codex C1), and the autopush 250ms /
    interactive 500ms wall-clock budget.

    Forensic-only invariant: any failure in this block is swallowed and
    breadcrumbed via ``mm: notice:``. The push proceeds.
    """
    if dry_run:
        return
    mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if mm_events_src is None:
        return
    try:
        budget_ms = (
            events.WALK_TIME_BUDGET_AUTOPUSH_MS if quiet else events.WALK_TIME_BUDGET_INTERACTIVE_MS
        )
        deadline = time.monotonic() + budget_ms / 1000.0
        events_dir = Path(mm_events_src["path"]).expanduser() / "events"

        roots, errs = events.discover_git_roots(config)
        since = events.last_push_ts(events_dir, device_id)

        g_rows = events.walk_git_projects(roots, since=since, total_budget_ms=budget_ms)
        for r in g_rows:
            r["device"] = device_id

        claude_paths = _enabled_claude_paths(sources)
        # Token cache wiring (v0.11.14+).
        # Step 1: decide whether to aggregate tokens this push (handles cold-
        # cache warm internally). Returns False on autopush + cold + no
        # detected upgrade transition — caller skips the token aggregation.
        do_token_walk = _decide_token_walk_policy(claude_paths, quiet=quiet)
        # Warm may have consumed several seconds. Refresh the deadline so
        # the session-metadata walk gets its full advertised budget instead
        # of an already-expired one (Codex outside-voice review caught this
        # — pre-fix, first interactive push / upgrade autopush could emit
        # an empty `projects: []` snapshot when warm ate the original
        # deadline).
        deadline = time.monotonic() + budget_ms / 1000.0

        agg_projects: list[dict] = []
        if do_token_walk:
            # Step 2: hold the token cache flock across the walk so
            # walk_session_metadata's per-file mutations to files dict are
            # captured atomically. "warn" mode under autopush degrades
            # gracefully on contention (can't get the lock → no token
            # aggregation this push); "block" under interactive (user is
            # waiting anyway).
            mode = "warn" if quiet else "block"
            with locked_json_rmw(
                token_usage.CACHE_PATH,
                default_factory=lambda: {"version": token_usage.CACHE_VERSION, "files": {}},
                on_contention=mode,
                contention_warning="token cache contended; skipping token aggregation",
            ) as ljson:
                if not ljson.is_locked:
                    files_dict = None
                else:
                    if ljson.data.get("version") != token_usage.CACHE_VERSION:
                        ljson.data.clear()
                        ljson.data.update({"version": token_usage.CACHE_VERSION, "files": {}})
                    if not isinstance(ljson.data.get("files"), dict):
                        ljson.data["files"] = {}
                    files_dict = ljson.data["files"]
                for claude_dir in claude_paths:
                    for row in events.walk_session_metadata(
                        claude_dir,
                        since=since,
                        deadline_monotonic=deadline,
                        token_cache_files=files_dict,
                    ):
                        agg_projects.extend(row.get("projects", []))
        else:
            for claude_dir in claude_paths:
                for row in events.walk_session_metadata(
                    claude_dir,
                    since=since,
                    deadline_monotonic=deadline,
                    token_cache_files=None,
                ):
                    agg_projects.extend(row.get("projects", []))
        s_rows: list[dict] = []
        if claude_paths:
            s_rows.append(
                {
                    "v": events.EVENTS_SCHEMA_VERSION,
                    "type": "sessions-snapshot",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "device": device_id,
                    "projects": agg_projects,
                }
            )

        source_names = [s["name"] for s in sources if isinstance(s.get("name"), str)]
        # Fleet-wide author-email trust set (v0.11.17). gather_local_identities
        # is cache-first: hot path is ~1ms; cold/stale path emits a single
        # `mm: notice: refreshing identity cache (one-off)` line and runs a
        # synchronous refresh inline (D1 from /plan-eng-review — the user
        # accepted the one-off slow path over budget contortions). Emitted
        # as `local_emails: []` (explicit empty) when this machine has no
        # configured identities — distinguishable from "pre-v0.11.17 peer
        # with no field at all" so the aggregator can choose its fallback.
        local_emails = identity.gather_local_identities(allow_refresh=True)
        mm_event = events.make_mm_push_event(
            device=device_id,
            mm_version=__version__,
            sources=source_names,
            discovery_errors=errs,
            local_emails=local_emails,
        )
        # CT-4 invariant: mm-push event LAST so a partial write doesn't
        # advance the next-push cursor.
        events.write_push_event(events_dir, device_id, [*g_rows, *s_rows, mm_event])

        if time.monotonic() > deadline:
            sys.stderr.write("mm: notice: events tail budget exceeded\n")
    except Exception as e:
        sys.stderr.write(f"mm: notice: events tail failed: {type(e).__name__}: {safe_str(e)}\n")


def _run_events_backfill(
    config: dict,
    sources: list[dict],
    device_id: str,
) -> None:
    """Init-time backfill of git+sessions events for the past 30 days.

    Mirrors ``_run_events_tail`` but writes only ``git-snapshot`` and
    ``sessions-snapshot`` rows — NO ``mm-push`` row. Two consequences:

    * Push-count semantics stay honest: an init-counted-as-push would
      inflate the per-window mm-push count in the retro by 1 on every
      fresh-install machine.
    * The cursor (``last_push_ts``) stays at "no prior mm-push" so the
      first real push walks the same 30-day range. Aggregator dedups via
      ``(canonical_remote_url, sha)`` so retro output is unchanged; cost
      is one extra ~500ms ``git log`` walk on the first push, paid once
      per machine.

    Idempotent at the aggregator layer (commits dedup; sessions latest-
    per-tuple wins). Forensic-only on failure: stderr breadcrumb, init
    proceeds.
    """
    mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if mm_events_src is None:
        return
    try:
        budget_ms = events.WALK_TIME_BUDGET_INTERACTIVE_MS
        deadline = time.monotonic() + budget_ms / 1000.0
        events_dir = Path(mm_events_src["path"]).expanduser() / "events"

        # Explicit 30-day window. last_push_ts() returns the same value
        # on first run, but stating intent at the call site makes the
        # backfill semantics legible without chasing a default.
        since = datetime.now(timezone.utc) - timedelta(days=events.INITIAL_CURSOR_LOOKBACK_DAYS)

        roots, _errs = events.discover_git_roots(config)
        g_rows = events.walk_git_projects(roots, since=since, total_budget_ms=budget_ms)
        for r in g_rows:
            r["device"] = device_id

        claude_paths = _enabled_claude_paths(sources)

        # Warm the token cache inline at init (v0.11.14+). One-time cost
        # at init time — kb already accepts init takes a few seconds.
        # Subsequent pushes inherit a warm cache.
        if claude_paths:
            try:
                token_usage.warm_token_cache_inline(claude_paths)
            except Exception as e:
                sys.stderr.write(
                    f"mm: notice: token cache warm at init failed: "
                    f"{type(e).__name__}: {safe_str(e)}\n"
                )

        # Refresh deadline after warm — the warm can spend ~5s, which
        # would otherwise leave an already-expired deadline for the
        # session-metadata walk and produce an empty `projects: []`
        # backfill on fresh installs (Codex outside-voice review caught
        # this; matches the same fix in `_run_events_tail`).
        deadline = time.monotonic() + budget_ms / 1000.0

        agg_projects: list[dict] = []
        # Hold the token cache lock across the walk so per-jsonl mutations
        # persist as part of the same R/M/W. Init is interactive, so use
        # blocking mode.
        if claude_paths:
            with locked_json_rmw(
                token_usage.CACHE_PATH,
                default_factory=lambda: {"version": token_usage.CACHE_VERSION, "files": {}},
                on_contention="block",
            ) as ljson:
                if ljson.data.get("version") != token_usage.CACHE_VERSION:
                    ljson.data.clear()
                    ljson.data.update({"version": token_usage.CACHE_VERSION, "files": {}})
                if not isinstance(ljson.data.get("files"), dict):
                    ljson.data["files"] = {}
                files_dict = ljson.data["files"]
                for claude_dir in claude_paths:
                    for row in events.walk_session_metadata(
                        claude_dir,
                        since=since,
                        deadline_monotonic=deadline,
                        token_cache_files=files_dict,
                    ):
                        agg_projects.extend(row.get("projects", []))
        s_rows: list[dict] = []
        if claude_paths:
            s_rows.append(
                {
                    "v": events.EVENTS_SCHEMA_VERSION,
                    "type": "sessions-snapshot",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "device": device_id,
                    "projects": agg_projects,
                }
            )

        rows_to_write = [*g_rows, *s_rows]
        if rows_to_write:
            events.write_push_event(events_dir, device_id, rows_to_write)

        # Warm the identity cache at init (v0.11.17, D5 from /plan-eng-review).
        # First push after init then has hot identity data and emits no
        # slow-path notice. Failure is forensic-only — backfill proceeds.
        try:
            identity.refresh_identity_cache(force=True)
        except Exception as e:
            sys.stderr.write(
                f"mm: notice: identity cache warm at init failed: "
                f"{type(e).__name__}: {safe_str(e)}\n"
            )

        if time.monotonic() > deadline:
            sys.stderr.write("mm: notice: events backfill budget exceeded\n")
    except Exception as e:
        sys.stderr.write(f"mm: notice: events backfill failed: {type(e).__name__}: {safe_str(e)}\n")


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

    # Group 8 / Track 8A retro-fleet skill self-heal. Position locked in
    # /plan-eng-review Architecture #5: AFTER device self-heal (storage-
    # write before any walk), BEFORE events tail (local-FS self-heals
    # stacked, events tail is the load-bearing always-runs block). Gated by
    # 24h-TTL — the marker stat is the entire hot-path cost on the steady-
    # state push (~1 syscall). dry_run gates the install too (preview
    # contract; mirrors _ensure_device_registered).
    if not dry_run and _skill_link_check_due():
        _ensure_retro_skill_link(dry_run=False)

    # Build local manifest (v2 with sources)
    sources = get_sources(config)
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

    # Track 7B head-position events tail. MUST run on every push attempt
    # past this point (events.py:19-22 trust boundary), BEFORE
    # build_manifest_v2 so this push's events file lands on disk in time
    # to be uploaded same push. See `_run_events_tail` for invariants.
    _run_events_tail(config, sources, device_id, dry_run=dry_run, quiet=quiet)

    skipped: list[tuple[str, str]] = []

    def on_skip(path: str, reason: str) -> None:
        skipped.append((path, reason))
        if verbose and not quiet:
            console.print(f"  [dim]skipped: {safe_str(path)} ({safe_str(reason)})[/dim]")

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
    exclude_map = _build_exclude_map(config)
    disabled_sources = list(config.get("sync", {}).get("disabled_sources", []) or [])
    if remote_manifest is not None:
        remote_manifest = _filter_disabled_sources(remote_manifest, disabled_sources)
        remote_manifest = _filter_excluded_paths(remote_manifest, exclude_map)

    # Generate tombstones for files that disappeared since last push
    tombstones = generate_tombstones(local_manifest, remote_manifest, device_id)
    local_manifest["tombstones"] = tombstones

    # Diff and upload per-source
    total_bytes = 0
    total_new = 0
    total_modified = 0
    total_deleted = 0

    # Only the REAL remote manifest drives the diff (avoid re-uploading every
    # file just because we're recovering from corruption via the sidecar).
    real_remote = fetch.manifest if fetch.is_ok else None
    remote_sources = real_remote.get("sources", {}) if real_remote else {}

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
            console.print("\n[bold]Dry run complete.[/bold]")
            console.print(f"  Completed in {elapsed:.1f}s")
        return None

    # If the remote manifest was corrupt and we recovered via sidecar/peers,
    # we MUST rewrite the remote manifest to heal the corruption - even when
    # local file diffs are zero. Otherwise the corrupt manifest stays in
    # place and recovered tombstones never propagate.
    recovering_from_corrupt = fetch.status == "corrupt"

    if not (total_new or total_modified or total_deleted) and not recovering_from_corrupt:
        if not quiet:
            console.print("[green]Nothing to push \u2014 everything is up to date.[/green]")
        return None

    # Upload manifest (includes tombstones)
    if not quiet:
        if recovering_from_corrupt and not (total_new or total_modified or total_deleted):
            console.print("\n[bold]Rewriting manifest to heal remote corruption...[/bold]")
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
            "'keep-both' (default): auto-rename local to .sync-conflict-*, "
            "remote wins canonical. 'prompt': ask per-file. 'fail': preflight "
            "all files and exit 2 (no writes) if any would conflict -- for CI."
        ),
        case_sensitive=False,
    ),
) -> None:
    """Pull session data from storage to local.

    Conflicts (local edited, remote differs) resolve per `--conflict-mode`:
    - keep-both (default): remote wins canonical, local renamed to
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
    from packaging.version import InvalidVersion, Version

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
    _find_conflict_files(config, migrate_pre_inversion=True)

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
    exclude_map = _build_exclude_map(config)
    disabled_sources = list(config.get("sync", {}).get("disabled_sources", []) or [])
    if disabled_sources:
        manifest_cache = {
            did: (None if m is None else _filter_disabled_sources(m, disabled_sources))
            for did, m in manifest_cache.items()
        }
    if exclude_map:
        filtered_cache: dict[str, dict | None] = {}
        for did, m in manifest_cache.items():
            if m is None:
                filtered_cache[did] = None
                continue
            filtered = _filter_excluded_paths(m, exclude_map)
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
    console.print(f"  Device: {device_name} ({device_id})")

    # Surface the last autopull/autopush breadcrumb so a wedged sync
    # (silent lock contention, missing passphrase, bad config) is visible.
    breadcrumb_path = _autorun_breadcrumb_path()
    if breadcrumb_path.exists():
        try:
            import json as _json

            crumb = _json.loads(breadcrumb_path.read_text())
            ts = crumb.get("timestamp", "?")
            verb = crumb.get("verb", "?")
            outcome = crumb.get("outcome", "?")
            detail = crumb.get("detail")
            outcome_str = f"{outcome}: {detail}" if detail else outcome
            console.print(f"  Last auto-{verb}: {ts} ({outcome_str})")
        except (OSError, ValueError):
            pass  # corrupt breadcrumb is not worth surfacing an error for
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
            f"{', '.join(missing_excludes)} — run [bold]mm migrate-config[/bold] to add."
        )

    # Seam 3 — auto-upgrade nudge surfacing in status. Reads cache only,
    # no network call. Distinct from autopull/autopush emission (which gates
    # on last_nudged_at) — `mm status` is an explicit user check and shows
    # the cached result every time, regardless of the 24h re-emit gate.
    upgrade_result = upgrade.check_for_upgrade(config)
    if upgrade_result.state == "upgrade-available" and upgrade_result.latest:
        console.print(
            f"  [yellow]Upgrade available:[/yellow] "
            f"{upgrade_result.local} → {upgrade_result.latest} "
            f"(run [bold]{upgrade_result.install_cmd}[/bold])"
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
        console.print(
            f"  [yellow]Disabled sources (this device):[/yellow] "
            f"{', '.join(sorted(disabled_list))} — "
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
            f"  [cyan]New source available:[/cyan] {name} — "
            f"run [bold]mm enable-source {name}[/bold] to sync, "
            f"or [bold]mm disable-source {name}[/bold] to dismiss."
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

        console.print(f"\n  [bold]Source '{src_name}':[/bold]")
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
    elif not source:
        console.print("\n  [green]All sources in sync.[/green]")

    if len(devices) > 1:
        console.print("\n  Other devices:")
        for d in devices:
            if d["device_id"] != device_id:
                console.print(f"    {d['device_name']} ({d['device_id']})")


# ── diag ──────────────────────────────────────────────────────────────


def _collect_diag_state(backend: LocalBackend) -> dict:
    """Gather non-secret state for support triage.

    Secrets allowlist — NEVER include:
      * raw root_salt bytes (fingerprint only)
      * master_key (never computed here)
      * keycheck_blob contents
      * passphrase
      * peer device_ids (only counts)

    Uses existing tri-state helpers (`fetch_crypto_init`, `sidecar.read`)
    rather than re-sampling raw blob bytes — the tri-state branches are
    where corruption meaning lives, so bypassing them would misreport the
    exact scenario this command exists to diagnose.
    """
    import json as _json

    # Local config (best-effort — a broken config is itself diag-worthy).
    try:
        cfg = load_config()
        dev_id = cfg.get("device", {}).get("id")
        dev_name = cfg.get("device", {}).get("name")
        storage_path = cfg.get("storage", {}).get("path")
        local_fp = cfg.get("crypto", {}).get("root_salt_fp")
        config_state = "ok"
    except MindMeldError as e:
        dev_id = dev_name = storage_path = local_fp = None
        config_state = f"error: {e}"

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
    # last fire and how did it end).
    breadcrumb: dict | None = None
    try:
        bp = _autorun_breadcrumb_path()
        if bp.exists():
            breadcrumb = _json.loads(bp.read_text())
    except (OSError, ValueError):
        breadcrumb = {"error": "unreadable"}

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
        console.print(f"  verb:       {br.get('verb')}")
        console.print(f"  outcome:    {br.get('outcome')}")
        console.print(f"  timestamp:  {br.get('timestamp')}")
        if br.get("detail"):
            console.print(f"  detail:     {br.get('detail')}")


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
    dry_run: bool = typer.Option(False, "--dry-run", help="List orphans without deleting"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    prune_conflicts: bool = typer.Option(
        False,
        "--conflicts",
        help=f"Also delete .sync-conflict-* files older than {CONFLICT_AGE_DAYS} days",
    ),
) -> None:
    """Garbage collect orphaned blobs. Optionally reap stale conflict files."""
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
        _gc_old_event_files(config, dry_run, verbose)
        # v0.11.14+: token cache reaper. Stale entries (no living jsonl OR
        # by_day older than 90d) are dropped. Dry-run reports without
        # mutating the cache file.
        _gc_token_cache(dry_run, verbose)
        if prune_conflicts:
            _gc_old_conflict_files(config, dry_run, verbose)
    finally:
        release_lock()


def _gc_token_cache(dry_run: bool, verbose: bool) -> None:
    """Reap session-tokens.json entries with no living jsonl AND entries
    whose most recent by_day key is older than 90 days. Best-effort —
    cache reconstruction on the next push backstops a GC failure."""
    if dry_run:
        # Dry-run: count without mutating. Re-implement the predicate
        # cheaply via is_cache_cold + a peek.
        if not token_usage.CACHE_PATH.exists():
            if verbose:
                console.print("[dim]No token cache to gc.[/dim]")
            return
        if verbose:
            console.print("[dim]Token cache reaper: dry-run; skipping.[/dim]")
        return
    try:
        n = token_usage.gc_cache_entries()
    except Exception as e:
        sys.stderr.write(f"mm: notice: token cache gc failed: {type(e).__name__}: {safe_str(e)}\n")
        return
    if verbose and n:
        console.print(f"[dim]Reaped {n} stale token cache entr{'y' if n == 1 else 'ies'}.[/dim]")


def _sweep_local_tmp_files(
    backend: LocalBackend,
    my_device_id: str,
    dry_run: bool,
    verbose: bool,
) -> int:
    """Reap stale tmp*.tmp left by crashed atomic_write_bytes calls.

    Scoped strictly to THIS device's subtrees:
        <root>/data/<my_device_id>/
        <root>/manifests/<my_device_id>/

    Peer subtrees are never touched — the iCloud storage tree is shared
    across machines but flock only serializes THIS Mac, so a file in
    another device's subtree might be in the middle of being uploaded
    by their iCloud daemon. Not our garbage to collect.

    NOTE: devices/ is intentionally EXCLUDED. It is a flat directory
    shared across machines (no per-device subdir), and tempfile.mkstemp
    names are random — there is no reliable way to tell this device's
    stranded tmp from a peer's in-flight write. The rare leak there is
    accepted; see Track 3A GC sweep for global orphan reaping.

    Returns the count swept (or would-be-swept if dry_run).
    """
    count = 0
    scoped_dirs = [
        backend.root / "data" / my_device_id,
        backend.root / "manifests" / my_device_id,
    ]
    victims: list[Path] = []
    for base in scoped_dirs:
        if not base.exists():
            continue
        for p in base.rglob("tmp*.tmp"):
            if p.is_file():
                victims.append(p)

    # devices/ deliberately excluded — see docstring.

    for v in victims:
        if dry_run:
            if verbose:
                console.print(f"  [dim]would sweep: {v}[/dim]")
        else:
            try:
                v.unlink()
            except OSError as e:
                if verbose:
                    console.print(f"  [yellow]sweep failed: {v} — {e}[/yellow]")
                continue
        count += 1

    if count > 0 and not dry_run:
        console.print(f"  [dim]swept {count} stale tmp files[/dim]")
    elif count > 0 and dry_run:
        console.print(f"  [dim]would sweep {count} stale tmp files[/dim]")
    return count


def _do_gc(
    config: dict,
    passphrase: str,
    memory_kb: int,
    dry_run: bool,
    verbose: bool,
) -> int:
    """Run garbage collection. Returns number of orphaned blobs found/deleted."""
    backend = get_backend(config)
    my_device_id = config["device"]["id"]

    # Sweep this device's stale tmp*.tmp files before ref-counting.
    # Runs UNDER the caller's lock (acquire_lock already held by gc()),
    # so no concurrent writer can race with the sweep.
    _sweep_local_tmp_files(backend, my_device_id, dry_run, verbose)

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

    from mind_meld.manifest import _is_excluded as _manifest_is_excluded
    from mind_meld.manifest import walk_source

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
    import difflib

    matches = difflib.get_close_matches(name, valid, n=1, cutoff=0.6)
    suggestion = f" Did you mean '{matches[0]}'?" if matches else ""
    raise ConfigError(
        f"unknown source '{name}' — valid: {', '.join(valid)}.{suggestion} "
        "Use --force to accept a name not yet known to mm "
        "(forward-compat for not-yet-shipped sources)."
    )


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
        console.print(f"[dim]Source '{name}' is already disabled.[/dim]")
        return

    disabled.append(name)
    patch_config_on_disk({"sync": {"disabled_sources": sorted(disabled)}})
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
    """
    config = _get_config()
    try:
        _validate_source_name(name, config, force=force)
    except ConfigError as e:
        _error(str(e))

    sync = dict(config.get("sync", {}) or {})
    disabled = list(sync.get("disabled_sources", []) or [])
    explicit_sources = list(sync.get("sources", []) or [])
    explicit_names = [s["name"] for s in explicit_sources]
    default_names = [s["name"] for s in DEFAULT_SOURCES]

    updates: dict[str, Any] = {}

    if name in disabled:
        disabled.remove(name)
        updates["disabled_sources"] = sorted(disabled)

    # If the user has explicit sources and this name isn't among them,
    # but it IS in DEFAULT_SOURCES, append the default so enable actually
    # has effect (auto-detect doesn't fire when explicit sources are set).
    needs_explicit_append = (
        explicit_sources and name not in explicit_names and name in default_names
    )
    if needs_explicit_append:
        default = get_default_source(name)
        if default is not None:
            explicit_sources.append(default)
            updates["sources"] = explicit_sources

    if not updates:
        # Already enabled and configured — no-op message.
        if name in explicit_names or (not explicit_sources and name in default_names):
            console.print(f"[dim]Source '{name}' is already enabled.[/dim]")
            return

    if updates:
        patch_config_on_disk({"sync": updates})

    _record_seen([name])
    console.print(f"[green]Enabled source '{name}' on this device.[/green]")


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
            currently_active = (
                iname in explicit_names or (not explicit_sources and iname in default_names)
            ) and iname not in disabled
            answered = _prompt_source_toggle(item, current_state=currently_active)
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

    patch_config_on_disk({"sync": updates})

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
    if not diffs:
        console.print("[green]Config is already up to date.[/green]")
        return

    console.print("\n[bold]Recommended exclude_patterns updates:[/bold]")
    for name, missing, current in diffs:
        console.print(f"\n  [bold]{name}[/bold]  (current: {current!r})")
        for p in missing:
            console.print(f"    [green]+ {p}[/green]")

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
            if src.get("name") in diff_map:
                merged = dict(src)
                current = list(src.get("exclude_patterns") or [])
                merged["exclude_patterns"] = current + diff_map[src["name"]]
                new_sources.append(merged)
            else:
                new_sources.append(src)

        # patch_config_on_disk replaces the sources array wholesale (per its
        # contract). max_file_size and other [sync] scalars survive because
        # the section-level merge is per-field.
        patch_config_on_disk({"sync": {"sources": new_sources}})
        # Clear any prior migration breadcrumb — config now matches.
        breadcrumb = _migration_state_path()
        try:
            breadcrumb.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        console.print(
            f"[green]Updated {len(diffs)} source(s).[/green] "
            f"Config written to {_config_module.CONFIG_PATH}."
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
    entry and proposes adding any missing recommended globs. Idempotent —
    re-running on a fully-migrated config exits with "already up to date".

    Acquires the mm lockfile so a concurrent push/pull can't read a half-
    written config.
    """
    _migrate_config_core(yes=yes, dry_run=dry_run)


# ── install-skills ────────────────────────────────────────────────────


@app.command(name="install-skills")
def install_skills_cmd() -> None:
    """Install (or re-install) the retro-fleet Claude Code skill symlink.

    Force-runs the same self-heal that ``mm init`` and ``mm push`` invoke
    automatically, bypassing the steady-state TTL gate. Intended for:

    * post-cleanup recovery (link removed by hand, e.g. after deleting an
      old pipx workspace whose path the link pointed at)
    * manual install on a machine where ``mm push`` hasn't run yet
    * verifying the link state on a fresh ``pipx install`` of mm

    The symlink target follows the wheel: pipx upgrades replace the
    contents of ``~/.local/pipx/venvs/mind-meld/`` in place, so the link
    auto-updates on every ``pipx upgrade mind-meld``.
    """
    target = Path("~/.claude/skills").expanduser() / _SKILL_LINK_NAME
    skills_dir = target.parent
    if not skills_dir.exists():
        typer.echo(
            f"mm: error: {skills_dir} does not exist; install Claude Code first",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        skill_src = _resolve_retro_skill_src()
    except Exception as e:
        typer.echo(
            f"mm: error: skill source unresolvable: {type(e).__name__}: {safe_str(e)}",
            err=True,
        )
        raise typer.Exit(code=1) from e

    _ensure_retro_skill_link(dry_run=False)

    if target.is_symlink() and target.exists():
        try:
            if target.resolve() == skill_src.resolve():
                typer.echo(f"Installed: {target} -> {skill_src}")
                return
        except OSError:
            pass

    if target.exists() or target.is_symlink():
        typer.echo(
            f"mm: error: {target} exists and is not mm's symlink; "
            f"remove it and re-run (mm's source is {skill_src})",
            err=True,
        )
    else:
        typer.echo(
            "mm: error: install did not complete (see stderr above for details)",
            err=True,
        )
    raise typer.Exit(code=1)


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


def _synced_scan_dirs(src_cfg: dict, base_path: Path) -> list[Path]:
    """Return the directories `mm push` would walk for this source.

    Limits conflict discovery to paths mm actually syncs so we don't
    list .sync-conflict-* files from unsynced areas (e.g., ~/.claude/sessions
    when the claude source only syncs memory/ and todos/).

    - claude type: projects/<any>/memory, projects/<any>/todos
    - generic type: include_dirs (relative to source root)
    """
    src_type = src_cfg.get("type", "claude")
    if src_type == "claude":
        from mind_meld.manifest import SYNCED_SUBDIRS

        projects = base_path / "projects"
        if not projects.exists():
            return []
        dirs: list[Path] = []
        for project_dir in projects.iterdir():
            if not project_dir.is_dir():
                continue
            for sub in SYNCED_SUBDIRS:
                candidate = project_dir / sub
                if candidate.exists():
                    dirs.append(candidate)
        return dirs
    # generic: include_dirs (resolved) + base for single-file includes
    dirs = []
    for d in src_cfg.get("include_dirs", []):
        candidate = base_path / d
        if candidate.exists():
            dirs.append(candidate)
    return dirs


def _inversion_marker_path() -> Path:
    """Canonical path for the one-shot inversion-install timestamp file."""
    return sidecar.SIDECAR_DIR / "inversion-installed-at"


def _ensure_inversion_marker() -> float | None:
    """Get-or-create the inversion-install timestamp (epoch seconds).

    Returns the timestamp as a float, or None on any read/parse/write
    failure (fail-safe: the caller treats None as "skip migration").

    Critical safety property: distinguishes pre-inversion conflict files
    (mtime predates the marker — produced by pre-v0.9.2 code on this
    machine) from post-inversion conflict files (mtime is at-or-after
    the marker — produced by THIS version's `_apply_conflict`, which
    emits unprefixed filenames). Without this gate, the migration sweep
    re-tags every fresh post-inversion sidecar as `v0-` on the next
    pull and resolve silently dispatches them backwards, causing data
    loss (the CRITICAL bug caught by /ship pre-landing review and
    independently confirmed by both adversarial and reviewer subagents).

    First-call semantics: writes the marker at "now" so any pre-existing
    `.sync-conflict-*` files already on disk (mtime < now) get migrated
    on this pull, and every NEW conflict file produced from here on
    (mtime > now) is correctly skipped.

    Best-effort: directory creation and file write may fail (perms,
    disk full). Failure returns None — the caller MUST treat None as
    "do not migrate" rather than "migrate everything", so a broken
    marker degrades to safe-default-no-migration instead of mass
    re-tagging.
    """
    path = _inversion_marker_path()
    try:
        if path.exists():
            return float(path.read_text().strip())
        sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        marker_ts = time.time()
        # Atomic write so a crash mid-create doesn't leave a partial
        # number that fails to float-parse on the next read.
        fsutil.atomic_write_bytes(
            path,
            f"{marker_ts}\n".encode(),
            fsync=True,
            mode=0o600,
        )
        return marker_ts
    except (OSError, ValueError, StorageError):
        return None


def _migrate_pre_inversion_conflict(path: Path) -> Path:
    """Rename a pre-inversion conflict file to carry the `v0-` prefix.

    Idempotent: a path already prefixed with `v0-` returns unchanged.
    Skips files whose mtime is at-or-after the inversion-install marker
    (those are post-inversion files produced by THIS version's
    `_apply_conflict`, which emits unprefixed filenames — re-tagging
    them as `v0-` would silently invert resolve's dispatch and cause
    data loss). Failure (rename error, target collision, missing marker)
    logs a warning to stderr and returns the original path so the
    caller can keep walking — losing one migration attempt is preferable
    to aborting the whole conflict-discovery sweep.

    MUST only be called from a lock-protected context (mm pull, mm
    resolve). `mm conflicts` is intentionally read-only and lockless;
    renaming there would race with autopull's own discovery walk
    (codex-2 #5).
    """
    from mind_meld.manifest import (
        CONFLICT_V0_PREFIX,
        is_pre_inversion_conflict_filename,
    )

    name = path.name
    if is_pre_inversion_conflict_filename(name):
        return path
    if not is_conflict_filename(name):
        return path

    # Mtime gate (5E ship-fix): only migrate files whose mtime predates
    # the inversion-install marker. Without this, fresh post-inversion
    # sidecars produced by `_apply_conflict` (which has the same
    # unprefixed shape as legacy pre-inversion files) get false-tagged
    # `v0-` on the very next pull, then `_resolve_interactive_loop`
    # dispatches them backwards (silent data loss).
    marker_ts = _ensure_inversion_marker()
    if marker_ts is None:
        # Fail-safe: marker unreadable / unwriteable. Refuse to migrate
        # rather than risk mis-tagging.
        return path
    try:
        file_mtime = path.stat().st_mtime
    except OSError:
        return path
    if file_mtime >= marker_ts:
        # Post-inversion file — produced after this version was installed.
        # Leave its filename unprefixed (the resolve dual-mode dispatch
        # treats no-prefix as post-inversion semantics).
        return path

    idx = name.find(CONFLICT_INFIX)
    if idx == -1:
        return path  # defensive — is_conflict_filename guarantees presence
    before = name[: idx + len(CONFLICT_INFIX)]
    after = name[idx + len(CONFLICT_INFIX) :]
    new_name = f"{before}{CONFLICT_V0_PREFIX}{after}"
    new_path = path.with_name(new_name)
    if new_path.exists():
        return path  # collision — leave both copies in place for resolve
    try:
        path.rename(new_path)
    except OSError as e:
        stderr_console.print(
            f"[yellow]warning:[/yellow] failed to migrate pre-inversion conflict file {path} — {e}"
        )
        return path
    return new_path


def _find_conflict_files(
    config: dict,
    *,
    migrate_pre_inversion: bool = False,
) -> list[tuple[str, Path, Path | None]]:
    """Walk all sync sources looking for .sync-conflict-* files.

    Scoped to the same paths mm push walks — won't surface conflict files
    from unsynced areas of the source tree. Returns (source_name,
    conflict_path, canonical_path_if_exists). Canonical is None if the user
    has already deleted it.

    Two scan strategies, since `mm push` walks two surfaces per source:
      1. Recursive scan inside include_dirs (and claude SYNCED_SUBDIRS).
      2. Depth-0 sibling-glob for generic include_files entries — top-level
         single-file syncs whose conflict siblings live next to them, not
         inside `_synced_scan_dirs`' recursive surface. Without (2), conflict
         files for top-level entries like ~/.gstack/retro-context.md are invisible
         to `mm conflicts` / `mm resolve` / `mm gc --conflicts` (the
         2026-04-24 first-pull bug — listed 5 of 6 conflicts).

    `migrate_pre_inversion` (default False): if True, rename any
    pre-inversion conflict files to carry the `v0-` prefix before
    returning. Lock-protected callers ONLY (mm pull, mm resolve).
    Pass False from `mm conflicts` (read-only; lockless — would race
    autopull) and from `_gc_old_conflict_files` (mtime-based reaping
    doesn't need the prefix discrimination, codex-2 #5).

    Dedup: scan strategies (1) and (2) overlap when an `include_files`
    entry sits inside an `include_dirs` directory (e.g. user customizes
    `include_files: ["projects/notes.md"]` AND `include_dirs:
    ["projects"]`). Without dedup, `mm conflicts` shows duplicate rows
    and `mm gc --conflicts` double-counts reaped files. Key is
    `(src_name, conflict_path)` not bare `Path`: two configured sources
    could legitimately reference overlapping subtrees, and dedup must
    preserve source attribution.
    """
    hits: list[tuple[str, Path, Path | None]] = []
    # Group 7 preflight #3 + D6: dedup key uses filesystem identity
    # (src_name, st_dev, st_ino) when stat succeeds — handles APFS
    # case-mismatched config (e.g. include_dirs ["projects"] +
    # include_files ["Projects/notes.md"]) correctly. Falls back to
    # (src_name, str(path)) when stat fails (race window between glob
    # and dedup) so we never silently drop a conflict file just because
    # of a transient stat error. The src_name component preserves source
    # attribution when two configured sources legitimately reference
    # overlapping subtrees.
    seen: set[tuple[str, int, int] | tuple[str, str]] = set()

    def _maybe_migrate(p: Path) -> Path:
        if migrate_pre_inversion:
            return _migrate_pre_inversion_conflict(p)
        return p

    def _identity_key(src_name: str, conflict_path: Path) -> tuple[str, int, int] | tuple[str, str]:
        try:
            st = conflict_path.stat()
        except OSError:
            return (src_name, str(conflict_path))
        return (src_name, st.st_dev, st.st_ino)

    def _try_add(src_name: str, conflict_path: Path, canonical: Path | None) -> None:
        key = _identity_key(src_name, conflict_path)
        if key in seen:
            return
        seen.add(key)
        hits.append((src_name, conflict_path, canonical))

    for src_cfg in get_sources(config):
        base_path = Path(src_cfg["path"]).expanduser().resolve()
        if not base_path.exists():
            continue

        # (1) Recursive scan in include_dirs / SYNCED_SUBDIRS.
        for scan_dir in _synced_scan_dirs(src_cfg, base_path):
            # rglob is loose (substring); filter strictly via is_conflict_filename
            # so user files like notes.sync-conflict-log.md are not listed/reaped.
            for conflict_path in scan_dir.rglob(f"*{CONFLICT_INFIX}*"):
                if not conflict_path.is_file():
                    continue
                if not is_conflict_filename(conflict_path.name):
                    continue
                conflict_path = _maybe_migrate(conflict_path)
                canonical = _canonical_for_conflict(conflict_path)
                _try_add(
                    src_cfg["name"],
                    conflict_path,
                    canonical if canonical.exists() else None,
                )

        # (2) Depth-0 sibling-glob for include_files entries. Gate on data
        # presence (not source type) so a future schema that adds
        # include_files to other source types doesn't silently lose
        # conflict visibility — the same scope-mismatch class of bug as
        # the original Track 5A Task 2.
        if src_cfg.get("include_files"):
            for filename in src_cfg.get("include_files", []):
                canonical = base_path / filename
                # parent_dir handles both top-level entries (parent == base_path)
                # and nested entries like "subdir/file.txt" (parent == base/subdir).
                # .glob() is depth-0 — never recurses into unsynced subtrees.
                parent_dir = canonical.parent
                if not parent_dir.exists():
                    continue
                pattern = f"{canonical.stem}{CONFLICT_INFIX}*{canonical.suffix}"
                for conflict_path in parent_dir.glob(pattern):
                    if not conflict_path.is_file():
                        continue
                    if not is_conflict_filename(conflict_path.name):
                        continue
                    conflict_path = _maybe_migrate(conflict_path)
                    _try_add(
                        src_cfg["name"],
                        conflict_path,
                        canonical if canonical.exists() else None,
                    )
    return hits


def _canonical_for_conflict(conflict_path: Path) -> Path:
    """Given a .sync-conflict-<ts>-<device>.<ext> path, return the canonical sibling.

    Strips the ".sync-conflict-<rest>" infix from the filename, re-assembling
    the original stem and extension. Uses rfind so that files which already
    had an infix before mm added its own (e.g., a Syncthing conflict file
    that mm then conflicted again) unwind the most recent layer only.
    """
    name = conflict_path.name
    idx = name.rfind(CONFLICT_INFIX)
    if idx == -1:
        return conflict_path
    before = name[:idx]
    # Everything after the infix up to the final suffix is conflict metadata.
    after = name[idx + len(CONFLICT_INFIX) :]
    suffix = ""
    if "." in after:
        suffix = "." + after.rsplit(".", 1)[-1]
    return conflict_path.with_name(before + suffix)


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
    hits = _find_conflict_files(config)
    if not hits:
        console.print("[green]No conflict files.[/green]")
        return

    from mind_meld.manifest import is_pre_inversion_conflict_filename

    table = Table(title=f"Conflict files ({len(hits)})")
    table.add_column("Source")
    table.add_column("Mode")
    table.add_column("local", no_wrap=False, overflow="fold")
    table.add_column("remote", no_wrap=False, overflow="fold")
    table.add_column("Age")
    now = datetime.now(timezone.utc)
    pre_inversion_seen = False
    for src_name, cpath, canonical in sorted(hits, key=lambda h: str(h[1])):
        try:
            mtime = datetime.fromtimestamp(cpath.stat().st_mtime, tz=timezone.utc)
            age = now - mtime
            age_str = f"{age.days}d" if age.days else f"{age.seconds // 3600}h"
        except OSError:
            age_str = "?"
        is_pre = is_pre_inversion_conflict_filename(cpath.name)
        if is_pre:
            pre_inversion_seen = True
            mode = "[yellow]pre-v0.9.2[/yellow]"
            # Pre-inversion: sidecar = local, canonical = remote.
            local_display = str(cpath)
            remote_display = str(canonical) if canonical else "[dim](gone)[/dim]"
        else:
            mode = "v0.9.2+"
            # Post-inversion: canonical = local, sidecar = remote.
            local_display = str(canonical) if canonical else "[dim](gone)[/dim]"
            remote_display = str(cpath)
        table.add_row(src_name, mode, local_display, remote_display, age_str)
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
    import secrets as _secrets
    from datetime import datetime, timezone

    mkey = manifest_key(device_id)
    src = storage_root / mkey
    if not src.exists():
        raise FileNotFoundError(str(src))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    candidates = [
        src.with_name(src.name + f".corrupt-{ts}"),
        src.with_name(src.name + f".corrupt-{ts}-{_secrets.token_hex(2)}"),
    ]
    dst = next((c for c in candidates if not c.exists()), None)
    if dst is None:
        # Both collided — extremely improbable, but pick a guaranteed-unique name.
        dst = src.with_name(src.name + f".corrupt-{ts}-{_secrets.token_hex(4)}")

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
      (l)ocal / (r)emote / (s)kip [default] / (a)bort.

    (l)ocal keeps your edits on this machine and discards the bytes from
    the other machine.
    (r)emote keeps the bytes from the other machine and discards your
    local edits on this conflict.
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
        hits = _find_conflict_files(config, migrate_pre_inversion=True)

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
        _, failed = _resolve_interactive_loop(hits, devices)
    finally:
        release_lock()

    if failed:
        # Surface partial-failure as a non-zero exit so CI / scripts driving
        # `mm resolve` can detect that some conflicts were not actually
        # resolved (rename/unlink/read errors mid-walk). Walk continues
        # through every conflict; only the exit code reflects the failure.
        raise typer.Exit(1)


def _resolve_interactive_loop(
    hits: list[tuple[str, Path, Path | None]],
    devices: list[dict[str, Any]] | None = None,
) -> tuple[int, int]:
    """Walk each conflict and prompt for resolution. Extracted so `resolve`
    stays a thin wrapper around acquire/release lock boilerplate.

    ``devices`` is the cached device list from ``list_devices(backend)``,
    used by the REMOTE banner to attribute conflict bytes to a peer name.
    None disables attribution -- legacy callers and unit tests can pass
    ``None`` (or omit the arg) and get an "(unknown peer)" annotation.
    Cache hoisted at the loop entry so a multi-conflict walk doesn't N+1
    on iCloud cold-cache reads.

    Returns (resolved, failed). `failed` covers per-conflict OSErrors
    (rename/unlink/read) that left the conflict file in place. `resolve`
    uses the failure count to decide its exit code; the walk itself does
    not abort on per-file errors (so the user gets to triage every conflict
    in one pass).
    """
    import difflib

    from mind_meld.conflictdiff import (
        count_divergent_lines,
        render_banner,
        render_prompt,
    )
    from mind_meld.manifest import (
        is_pre_inversion_conflict_filename,
        parse_conflict_device_short,
    )
    from mind_meld.merge import lcs_merge

    devices = devices or []
    resolved = 0
    failed = 0
    for src_name, cpath, canonical in hits:
        console.print(
            f"\n[bold yellow]Conflict in {safe_str(src_name)}:[/bold yellow] {safe_str(cpath)}"
        )

        if canonical is None:
            # Dual-mode preface by filename prefix. Pre-inversion (`v0-`)
            # files were produced when sidecar = local bytes; post-inversion
            # files have sidecar = remote bytes. The promote/delete ops are
            # the same; only the preface wording flips.
            if is_pre_inversion_conflict_filename(cpath.name):
                console.print(
                    "  [dim]No canonical file exists. This pre-v0.9.2 "
                    "conflict file holds your LOCAL edits from before "
                    "the conflict was created.[/dim]"
                )
            else:
                console.print(
                    "  [dim]No canonical file exists. This conflict file "
                    "holds REMOTE bytes from another machine.[/dim]"
                )
            console.print(
                "  [dim]Promote it to make it the canonical file, "
                "delete it to discard, or skip to leave it for later.[/dim]"
            )
            choice = (
                typer.prompt(
                    "  (p)romote / (d)elete / (s)kip",
                    default="s",
                    show_default=False,
                )
                .strip()
                .lower()
            )
            # Exact-match dispatch (not startswith): "post"/"plan"/"description"
            # must not silently promote/delete. (codex /review v0.9.0)
            if choice in ("p", "promote"):
                target_canonical = _canonical_for_conflict(cpath)
                try:
                    cpath.rename(target_canonical)
                    console.print(
                        f"  [green]promoted[/green] "
                        f"{safe_str(cpath.name)} -> {safe_str(target_canonical.name)}"
                    )
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]promote failed:[/red] {safe_str(e)}")
                    failed += 1
            elif choice in ("d", "delete"):
                try:
                    cpath.unlink()
                    console.print(f"  [red]deleted[/red] {safe_str(cpath.name)}")
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]delete failed:[/red] {safe_str(e)}")
                    failed += 1
            # else: skip (default)
            continue

        # Dual-mode dispatch by filename prefix. `v0-` = pre-inversion
        # (sidecar HOLDS local bytes; canonical holds remote). No prefix =
        # post-inversion (canonical IS local; sidecar holds remote bytes).
        # Picking by prefix (not timestamp) is sound: post-inversion files
        # are produced by code that NEVER stamps the v0- prefix, and
        # pre-inversion files are migrated to the prefix at discovery time
        # by `_migrate_pre_inversion_conflict`. Mixed prefixes in one walk
        # are expected during migration.
        is_pre_inversion = is_pre_inversion_conflict_filename(cpath.name)
        mode: Literal["pre_inversion", "post_inversion"] = (
            "pre_inversion" if is_pre_inversion else "post_inversion"
        )

        try:
            canonical_bytes = canonical.read_bytes()
            cpath_bytes = cpath.read_bytes()
        except OSError as e:
            console.print(f"  [red]read failed:[/red] {safe_str(e)}")
            failed += 1
            continue

        canonical_text = canonical_bytes.decode("utf-8", errors="replace").splitlines()
        cpath_text = cpath_bytes.decode("utf-8", errors="replace").splitlines()

        # Try LCS-as-synthetic-base 3-way merge so the (m)erge prompt option
        # can offer a clean union of additive edits. lcs_merge respects the
        # inversion-mode argument order so the embedded `<<<<<<< local` /
        # `>>>>>>> remote` markers stay accurate even on v0- files. Binary
        # input (NUL byte) returns conflict_count = -1 -- suppress (m).
        if is_pre_inversion:
            merged_bytes, merge_conflicts = lcs_merge(cpath_bytes, canonical_bytes)
        else:
            merged_bytes, merge_conflicts = lcs_merge(canonical_bytes, cpath_bytes)
        merge_available = merge_conflicts >= 0

        # Banner attribution: pull the device-short out of the conflict
        # filename and look it up against the cached devices list.
        short = parse_conflict_device_short(cpath.name)
        peer_name: str | None = None
        ambiguous_count = 0
        if short is not None:
            match, count = lookup_device_by_short_id(devices, short)
            if match is not None:
                peer_name = match.get("device_name")
            elif count > 1:
                ambiguous_count = count

        # Diff label semantics:
        #   pre_inversion: canonical = remote, cpath = local.
        #   post_inversion: canonical = local, cpath = remote.
        if is_pre_inversion:
            from_text, to_text = canonical_text, cpath_text
            from_label = f"remote ({safe_str(canonical.name)})"
            to_label = f"local  ({safe_str(cpath.name)})"
            local_path_for_banner = cpath
            remote_path_for_banner = canonical
        else:
            from_text, to_text = canonical_text, cpath_text
            from_label = f"local  ({safe_str(canonical.name)})"
            to_label = f"remote ({safe_str(cpath.name)})"
            local_path_for_banner = canonical
            remote_path_for_banner = cpath

        # Color banners ABOVE the diff so the user can scan-identify which
        # side is which without parsing diff prefixes. Both peer-controlled
        # paths AND the peer-controlled device_name flow into render_banner,
        # which strips terminal escapes via safe_text before they reach the
        # terminal (closes the same trust boundary safe_str closes for
        # filenames).
        console.print(render_banner("local", local_path_for_banner.name, None))
        console.print(
            render_banner(
                "remote",
                remote_path_for_banner.name,
                peer_name,
                ambiguous_count=ambiguous_count,
            )
        )

        diff = list(
            difflib.unified_diff(
                from_text,
                to_text,
                fromfile=from_label,
                tofile=to_label,
                lineterm="",
                n=3,
            )
        )

        # Three-number divergence summary BEFORE the diff so the user
        # gets a glance at scale. count_divergent_lines returns counts
        # keyed to the diff's from/to sides, which differ across modes:
        # in pre-inversion the diff is remote->local, so m = remote-only
        # and n = local-only. Map to semantic local/remote counts before
        # rendering so the summary copy stays honest in both modes AND
        # the prompt's (drops N ...) annotations are mode-correct.
        # Replacements count as one of each (a 1-line change is "1 of
        # yours + 1 from peer", K=2) -- the wording is honest about that.
        m, n, k = count_divergent_lines(diff)
        if is_pre_inversion:
            local_only, remote_only = n, m
        else:
            local_only, remote_only = m, n
        if k:
            console.print(
                f"  [dim]{local_only} unique line"
                f"{'' if local_only == 1 else 's'} of yours; "
                f"{remote_only} unique line"
                f"{'' if remote_only == 1 else 's'} from peer; "
                f"{k} total diff lines.[/dim]"
            )

        if diff:
            # Diff CONTENT is peer-controlled bytes — render via safe_text()
            # so Rich strips terminal escapes (CSI/OSC/DCS) AND defangs
            # markup. Text() alone passes raw escapes through.
            for line in diff[:80]:
                if line.startswith("+") and not line.startswith("+++"):
                    console.print(safe_text(line, style="green"))
                elif line.startswith("-") and not line.startswith("---"):
                    console.print(safe_text(line, style="red"))
                else:
                    console.print(safe_text(line))
            if len(diff) > 80:
                console.print(f"  [dim]...({len(diff) - 80} more diff lines)[/dim]")
        else:
            console.print("  [dim](files differ but text diff is empty — likely binary)[/dim]")

        # Concrete-action prompt copy. Filenames pre-sanitized via safe_str
        # since render_prompt does plain f-string interpolation. (m)erge
        # is offered when the LCS attempt succeeded (binary content sets
        # merge_available=False); the default key flips to (m) when the
        # merged result is clean -- the user just hits Enter to accept.
        # Pass semantic local/remote line counts so render_prompt can
        # annotate (l)ocal / (r)emote with the consequential drop count.
        # Suppress the counts on empty-diff (binary) so the annotation
        # doesn't claim "drops 0 lines" when we couldn't actually compare.
        prompt_local_only: int | None = local_only if diff else None
        prompt_remote_only: int | None = remote_only if diff else None
        console.print(
            render_prompt(
                safe_str(canonical.name),
                safe_str(cpath.name),
                mode,
                merge_available=merge_available,
                merge_conflicts=max(merge_conflicts, 0),
                local_only_lines=prompt_local_only,
                remote_only_lines=prompt_remote_only,
            )
        )
        prompt_default = "m" if (merge_available and merge_conflicts == 0) else "s"
        choice = (
            typer.prompt("  Choice", default=prompt_default, show_default=False).strip().lower()
        )

        # Backward-compat (v0.9.0 BREAKING): old letters `c` / `f` are still
        # rejected loudly. They encoded directional ambiguity post-inversion
        # (real silent-data-loss risk -- "kept canonical" meant local OR
        # remote depending on inversion era). Exact-match (not startswith):
        # otherwise "cancel" / "continue" would trip the rejection.
        if choice in ("c", "f"):
            print(
                "mm: error: input letters 'c' and 'f' are no longer accepted. "
                "Use (l)ocal to keep your local edits or (r)emote to keep "
                "the other machine's bytes. (Old labels removed in v0.9.0.)",
                file=sys.stderr,
            )
            raise typer.Exit(1)

        # Pre-1.0 deprecation alias: `b` / `both` used to mean "keep both
        # files; no change" which is exactly what `(s)kip` does today. No
        # silent-data-loss risk in mapping it through; emit a notice once
        # so users learn the new letter, then perform skip semantics.
        # Exact-match: "back"/"browse"/"between" must NOT silently trip
        # the alias.
        if choice in ("b", "both"):
            print(
                "mm: notice: 'b' / 'both' now means 'skip'; use 's' going forward "
                "(alias removed at 1.0).",
                file=sys.stderr,
            )
            choice = "s"

        # Exact-match dispatch (not startswith): "leave" / "lookup" must
        # not silently keep local; "retry" / "remove" must not silently
        # delete the conflict file. (codex /review v0.9.0 — caught a real
        # silent-data-loss footgun the eng review missed.)
        if choice in ("l", "local"):
            if is_pre_inversion:
                # Pre-inversion: sidecar HOLDS local bytes — promote.
                try:
                    cpath.rename(canonical)
                    console.print(
                        f"  [green]kept local; promoted[/green] "
                        f"{safe_str(cpath.name)} -> {safe_str(canonical.name)}"
                    )
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]rename failed:[/red] {safe_str(e)}")
                    failed += 1
            else:
                # Post-inversion: canonical IS local — drop the remote sidecar.
                try:
                    cpath.unlink()
                    console.print(
                        f"  [green]kept local; discarded remote[/green] {safe_str(cpath.name)}"
                    )
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]delete failed:[/red] {safe_str(e)}")
                    failed += 1
        elif choice in ("r", "remote"):
            if is_pre_inversion:
                # Pre-inversion: canonical IS remote — drop the local sidecar.
                try:
                    cpath.unlink()
                    console.print(
                        f"  [green]kept remote; discarded local[/green] {safe_str(cpath.name)}"
                    )
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]delete failed:[/red] {safe_str(e)}")
                    failed += 1
            else:
                # Post-inversion: sidecar HOLDS remote bytes — promote over local.
                try:
                    cpath.rename(canonical)
                    console.print(
                        f"  [green]kept remote; promoted[/green] "
                        f"{safe_str(cpath.name)} -> {safe_str(canonical.name)}"
                    )
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]rename failed:[/red] {safe_str(e)}")
                    failed += 1
        elif choice in ("m", "merge"):
            # (m)erge accept: write merged_bytes to canonical, drop sidecar.
            # Refuse silently when merge_available is False -- (m) was not
            # offered, treat any "m" / "merge" string as skip rather than
            # writing potentially-empty bytes from the binary-skip branch.
            if not merge_available:
                console.print(
                    "  [dim]merge unavailable for this file; "
                    "skipped (both files left on disk)[/dim]"
                )
            else:
                try:
                    fsutil.atomic_write_bytes(canonical, merged_bytes, fsync=False)
                except (OSError, StorageError) as e:
                    console.print(
                        f"  [red]merge write failed:[/red] {safe_str(canonical.name)} — "
                        f"{safe_str(e)}"
                    )
                    failed += 1
                    continue
                # Sidecar unlink is best-effort: canonical already holds the
                # merged bytes, so a unlink failure is cosmetic. Stale
                # sidecars get reaped by `mm gc --conflicts` (30d TTL).
                try:
                    cpath.unlink()
                except OSError as e:
                    print(
                        f"mm: warning: merged result written; sidecar unlink "
                        f"failed: {safe_str(cpath.name)} — {safe_str(e)}",
                        file=sys.stderr,
                    )
                if merge_conflicts == 0:
                    console.print(
                        f"  [cyan]merged[/cyan] {safe_str(canonical.name)} (clean LCS merge)"
                    )
                else:
                    console.print(
                        f"  [cyan]merged[/cyan] {safe_str(canonical.name)} "
                        f"(contains {merge_conflicts} <<<<<<< region"
                        f"{'s' if merge_conflicts != 1 else ''}; "
                        f"resolve in editor)"
                    )
                resolved += 1
        elif choice in ("a", "abort"):
            raise typer.Abort()
        else:
            # Default-or-skip path -- includes (s)kip, plain Enter, and any
            # unrecognized input. Both files stay on disk; user can run
            # `mm resolve` later or delete the .sync-conflict-* manually.
            console.print("  [dim]skipped; both files left on disk[/dim]")

    if failed:
        console.print(f"\n[bold]Resolved {resolved} of {len(hits)}; {failed} failed.[/bold]")
    else:
        console.print(f"\n[bold]Resolved {resolved} of {len(hits)}.[/bold]")
    return resolved, failed


def _gc_old_event_files(config: dict, dry_run: bool, verbose: bool) -> int:
    """Reap mm-events JSONL files older than ``EVENTS_RETENTION_DAYS``.

    Track 7B fleet retention. The retro skill reads events by walking the
    synced manifest at retro time, so deletion via tombstone propagation
    is the fleet-wide retention mechanism: this device drops the file
    locally → next push generates a tombstone → all peers drop it on
    pull. An offline peer that comes back online sees the tombstone
    too, suppressing resurrection of the deleted day file.

    Reap by filename date (``<device>-YYYY-MM-DD.jsonl``), NOT mtime —
    iCloud restores can rewrite mtimes back to "now" while the filename
    date is intrinsic to the event-day boundary.

    Path resolution: from ``get_sources(config)`` so user-customized
    mm-events paths are honored. Returns 0 when no mm-events source is
    enabled / resolved.
    """
    sources = get_sources(config)
    mm_events_src = next((s for s in sources if s.get("name") == "mm-events"), None)
    if mm_events_src is None:
        return 0
    events_dir = Path(mm_events_src["path"]).expanduser() / "events"
    if not events_dir.is_dir():
        return 0

    today = datetime.now(timezone.utc).date()
    reaped = 0
    for path in events_dir.rglob("*-*.jsonl"):
        m = _EVENTS_FILENAME_DATE_RE.match(path.name)
        if m is None:
            # Non-conforming filename in the events tree — leave alone.
            continue
        try:
            file_date = datetime.strptime(m.group("date"), "%Y-%m-%d").date()
        except ValueError:
            continue
        age_days = (today - file_date).days
        if age_days < EVENTS_RETENTION_DAYS:
            continue
        if verbose or dry_run:
            prefix = "would delete" if dry_run else "deleted"
            console.print(f"  [dim]{prefix} (age {age_days}d):[/dim] {safe_str(path)}")
        if not dry_run:
            try:
                path.unlink()
                reaped += 1
            except OSError:
                pass
        else:
            reaped += 1
    label = "would reap" if dry_run else "reaped"
    console.print(
        f"[bold]{label}[/bold] {reaped} stale events files "
        f"(older than {EVENTS_RETENTION_DAYS} days)"
    )
    return reaped


def _gc_old_conflict_files(config: dict, dry_run: bool, verbose: bool) -> int:
    """Delete .sync-conflict-* files older than CONFLICT_AGE_DAYS. Returns count."""
    hits = _find_conflict_files(config)
    cutoff = datetime.now(timezone.utc) - timedelta(days=CONFLICT_AGE_DAYS)
    reaped = 0
    for src_name, cpath, _canonical in hits:
        try:
            mtime = datetime.fromtimestamp(cpath.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            if verbose or dry_run:
                age_days = (datetime.now(timezone.utc) - mtime).days
                prefix = "would delete" if dry_run else "deleted"
                console.print(f"  [dim]{prefix} (age {age_days}d):[/dim] {safe_str(cpath)}")
            if not dry_run:
                try:
                    cpath.unlink()
                    reaped += 1
                except OSError:
                    pass
    label = "would reap" if dry_run else "reaped"
    console.print(
        f"[bold]{label}[/bold] {reaped} stale conflict files (older than {CONFLICT_AGE_DAYS} days)"
    )
    return reaped


# ── auto commands (hook-safe: silent, never-prompt, typed errors) ─────


_AUTO_LOG_MAX_BYTES = 1_000_000
_AUTO_LOG_KEEP_BYTES = 512_000


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
    """Once-per-invocation interactive prompt for missing recommended excludes.

    Called from the top of `mm push` / `mm pull` ONLY (interactive verbs).
    Auto-commands (autopull/autopush) NEVER prompt and NEVER mutate config —
    they write a `migration-state.json` breadcrumb instead and let `mm status`
    surface the signal. Visible-failure contract: silent config mutation
    in a hook would be exactly the class of "wedged sync I never noticed"
    failure the contract exists to prevent.
    """
    missing = _config_missing_recommended_excludes(config)
    if not missing:
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        # Non-TTY interactive verb (CI, piped invocation): warn to stderr
        # but don't block on a prompt nobody can answer.
        stderr_console.print(
            f"[yellow]warning:[/yellow] config missing recommended excludes "
            f"for source(s) {', '.join(missing)}. Run "
            f"[bold]mm migrate-config[/bold] to add them."
        )
        return
    console.print(
        f"\n[yellow]Config is missing recommended exclude_patterns for "
        f"source(s):[/yellow] {', '.join(missing)}"
    )
    console.print(
        "  These per-machine artifacts (e.g. gstack repo-mode caches) "
        "produce churn on every pull when synced."
    )
    if typer.confirm("Run 'mm migrate-config' now?", default=True):
        # Call the core directly so typer's option machinery isn't in the
        # way; the inner "Apply these updates?" prompt still confirms
        # before writing.
        _migrate_config_core(yes=False, dry_run=False)


def _write_autorun_breadcrumb(verb: str, outcome: str, detail: str = "") -> None:
    """Record the last autopull/autopush attempt for forensic observability.

    Written on EVERY invocation -- success, lock-skip, config-missing,
    typed error, unexpected error. `mm status` surfaces this so a user can
    see 'last auto-sync attempt: 3h ago, skipped (lock held)' instead of
    wondering why sync appears wedged.

    Silent contract preserved: nothing is printed. Any failure here is
    swallowed -- a broken breadcrumb must never crash the hook.
    """
    try:
        sidecar.SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verb": verb,
            "outcome": outcome,
        }
        if detail:
            payload["detail"] = detail
        import json as _json

        _autorun_breadcrumb_path().write_text(_json.dumps(payload, indent=2))
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
    import fcntl
    import traceback

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
        print(f"mm: {verb} failed - {e}", file=sys.stderr)
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
        print(f"mm: {verb} skipped - {e}", file=sys.stderr)
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
        print(f"mm: {verb} failed - {e}", file=sys.stderr)
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


@app.command()
def autopull() -> None:
    """Pull changes silently. Designed for Claude Code -- no prompts, minimal output.

    Never prompts (`get_passphrase(non_interactive=True)`). Silent exit on:
    missing config, missing passphrase, lock contention. Loud exit (one-line
    stderr + traceback to `~/.config/mind-meld/autopull.log`) on: corrupt
    config, crypto init failure, unexpected bug inside `_pull_core`.
    """
    setup = _auto_command_setup("pull")
    if setup is None:
        return

    # Visible-failure contract: NEVER auto-mutate config. Record the
    # missing-excludes signal so `mm status` can surface it for the next
    # interactive run. This breadcrumb is intentionally orthogonal to the
    # autorun outcome — pull can succeed AND have a pending migration.
    _write_migration_breadcrumb(_config_missing_recommended_excludes(setup.config))

    try:
        acquire_lock()
    except LockError:
        # Silent to Claude (never block the hook), but leave a breadcrumb
        # so `mm status` can show repeated lock-skips -- a wedged flock
        # used to produce hours of silent no-ops with no signal.
        _write_autorun_breadcrumb("pull", "lock-held")
        return

    try:
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
    except typer.Exit:
        # 5E ship-fix: `_check_fleet_version_or_refuse` exits via
        # `_error()` → `typer.Exit(1)` on a mixed-version fleet refusal.
        # Without this branch, the typed exit lands in `except Exception`
        # below — autopull would log the full refusal traceback to
        # `autopull.log` and write a "failed" breadcrumb on every
        # Claude Code session start, masking the real signal. `_error`
        # already wrote the user-facing stderr line; just mark the
        # breadcrumb and exit.
        _write_autorun_breadcrumb("pull", "fleet-refused")
    except MindMeldError as e:
        print(f"mm: pull failed - {e}", file=sys.stderr)
        if _should_log_cause(e):
            _log_unexpected("pull", e)
        _write_autorun_breadcrumb("pull", "failed", str(e))
    except Exception as e:
        print(
            "mm: pull failed - unexpected error (see autopull.log)",
            file=sys.stderr,
        )
        _log_unexpected("pull", e)
        _write_autorun_breadcrumb("pull", "failed", type(e).__name__)
    finally:
        release_lock()


@app.command()
def autopush() -> None:
    """Push changes silently. Designed for Claude Code -- no prompts, minimal output.

    Never prompts (`get_passphrase(non_interactive=True)`). Silent exit on:
    missing config, missing passphrase, lock contention. Loud exit (one-line
    stderr + traceback to `~/.config/mind-meld/autopush.log`) on: corrupt
    config, crypto init failure, unexpected bug inside `_push_core`. No
    auto-GC on autopush (prevents blob-deletion hole).
    """
    setup = _auto_command_setup("push")
    if setup is None:
        return

    # Visible-failure contract: NEVER auto-mutate config from a hook.
    # Surface the missing-excludes signal via a breadcrumb so `mm status`
    # nudges the user on their next interactive run.
    _write_migration_breadcrumb(_config_missing_recommended_excludes(setup.config))

    try:
        acquire_lock()
    except LockError:
        _write_autorun_breadcrumb("push", "lock-held")
        return

    try:
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

        _write_autorun_breadcrumb("push", "success")

        # Seam 2 — auto-upgrade nudge emission at the TAIL (mirrors autopull).
        upgrade.emit_nudge_if_due(setup.config)
    except typer.Exit:
        # 5E ship-fix: same as autopull — typed `typer.Exit` from
        # `_error()` (e.g. corrupt-manifest recovery refusal) must NOT
        # be logged as an unexpected error. `_error` already wrote the
        # user-facing stderr line; just mark the breadcrumb.
        _write_autorun_breadcrumb("push", "refused")
    except MindMeldError as e:
        print(f"mm: push failed - {e}", file=sys.stderr)
        if _should_log_cause(e):
            _log_unexpected("push", e)
        _write_autorun_breadcrumb("push", "failed", str(e))
    except Exception as e:
        print(
            "mm: push failed - unexpected error (see autopush.log)",
            file=sys.stderr,
        )
        _log_unexpected("push", e)
        _write_autorun_breadcrumb("push", "failed", type(e).__name__)
    finally:
        release_lock()


# ── helpers ───────────────────────────────────────────────────────────


def _default_device_name() -> str:
    """Generate a default device name from hostname."""
    import socket

    return socket.gethostname()


if __name__ == "__main__":
    app()
