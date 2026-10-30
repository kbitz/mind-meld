"""Shared command setup, manifest comparison and sync-output context."""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import typer
from rich.progress import Progress, SpinnerColumn, TextColumn

from mind_meld import (
    __version__,
    crypto,
    manifest,
    pullplan,
    safety,
    sidecar,
    updateprogress,
    upgrade,
)
from mind_meld import config as _config_module
from mind_meld.config import (
    SourceResolution,
    get_default_source,
    get_sources,
    load_config,
    patch_config_on_disk,
)
from mind_meld.consoles import console, stderr_console
from mind_meld.crypto import (
    FORMAT_VERSION,
    CryptoInitFetch,
    _newer_version,
    decrypt,
    fetch_crypto_init,
    get_passphrase,
    load_master_key,
    root_salt_fingerprint,
    set_crypto_session,
    verify_passphrase,
)
from mind_meld.devices import (
    _list_devices_impl,
)
from mind_meld.errors import (
    KEY_HEADER_COPIES_URL,
    NEWER_STORAGE_URL,
    ConfigError,
    CryptoError,
    LockError,
    ManifestError,
    MindMeldError,
    NewerFormatError,
    SnapshotError,
    StorageNotFound,
    os_error_cause,
    snapshot_refusal,
)
from mind_meld.lockfile import acquire_lock, release_lock
from mind_meld.manifest import (
    MARKER_SKIP_NAME,
    MUSE_EXCLUDE_PATTERNS,
    DiffResult,
    _under_skip_prefix,
    _validate_rel_path,
    collect_tombstones,
    deserialize_manifest,
    diff_files,
    is_conflict_filename,
    is_tombstoned,
    load_manifest,
    marker_skip_globs,
    matches_include_patterns,
    mtime_from_manifest,
)
from mind_meld.manifest import (
    _is_excluded as _manifest_is_excluded,
)
from mind_meld.safety import safe_str
from mind_meld.storage.keys import manifest_key
from mind_meld.storage.local import LocalBackend

FetchStatus = Literal["ok", "missing", "corrupt"]


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

    `conflict_copies_only` marks an "ok" built only from conflict copies
    because the canonical copy is absent or could not be read. GC refuses
    it as incomplete own metadata; push treats it like a corrupt manifest
    only for its no-op gate, rewriting the canonical from local truth. Its
    prior merges the copies with the local sidecar and freezes only stored
    blobs; blob diffs use the copies alone. Other callers may use the merged
    copies.

    `vanished` (Track 87A) marks an observed canonical gone after bounded
    retries, or a missing result after a validated conflict copy disappears
    on its final read. Either can be a sync replacement in flight. Readers
    treat absence as "missing", never "corrupt". A writer must not treat it as a first push:
    `_recover_prior_manifest` sends a vanished own manifest through the
    sidecar and peer tombstones before it refuses.
    """

    status: FetchStatus
    manifest: dict | None = None
    newer_version: int | None = None
    conflict_copies_only: bool = False
    vanished: bool = False

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


def _error(msg: str) -> None:
    # Route to stderr so quiet-mode autopush/autopull don't leak error text
    # to stdout (violating the one-line-stderr contract). Inside an attended
    # push/pull outcome scope the message is stored instead, and
    # _sync_failure renders it once as the final line.
    state = _SYNC_OUTPUT.get()
    if state is None:
        stderr_console.print(f"[red]Error:[/red] {safe_str(msg)}")
    else:
        state["message"] = msg
    raise typer.Exit(1)


def _list_devices_warn(backend: LocalBackend) -> list[dict]:
    """list_devices variant that surfaces dropped entries to stderr.

    Loss of a peer's device entry is load-bearing during corrupt-manifest
    recovery: the peer's tombstones are unreachable without its device_id,
    so silent drops can mask a recoverable manifest (see docs/invariants/sync.md
    "Corrupt-manifest recovery"; blob-directory peer discovery is deferred in
    the roadmap). Emitting a warning
    per dropped entry at least makes the gap visible to support triage.

    Library callers (and direct tests) should continue to call
    `devices.list_devices` — that variant is intentionally silent so
    programmatic consumers don't spam stderr.
    """

    def _warn(key: str, reason: str) -> None:
        with _pause_sync_progress():
            stderr_console.print(
                "[yellow]Warning:[/yellow] dropped device entry "
                f"{safe_str(key)} — {safe_str(reason)}"
            )

    return _list_devices_impl(backend, on_drop=_warn)


def _get_config(*, read_only: bool) -> dict:
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
    if not read_only:
        upgrade.run_transition_hook(config)
    return config


def _stdin_is_terminal() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (OSError, ValueError):
        return False


def _get_passphrase_or_exit() -> str:
    try:
        return get_passphrase()
    except CryptoError as e:
        if (
            _SYNC_OUTPUT.get() is not None
            and "passphrase" in str(e).lower()
            and not _stdin_is_terminal()
        ):
            if "input cancelled" in str(e):
                with suppress(OSError, ValueError):
                    sys.stderr.write("\n")
            _error("Supply the passphrase securely using MINDMELD_PASSPHRASE, then retry once")
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
        _error(
            f"keyring backend failure: {type(e).__name__}"
            if _SYNC_OUTPUT.get() is not None
            else f"keyring backend failure: {type(e).__name__}: {e}"
        )
        raise


def _newer_format_message(version: int, subject: str = "mm-crypto-init") -> str:
    """One upgrade remedy for newer envelopes, owned by the shared command core."""
    return (
        f"crypto: {subject} was written by a newer mm (format 0x{version:02x}) "
        f"or is damaged; this Mac runs mm {__version__}. "
        f"Upgrade with: {upgrade.INSTALL_CMD}. "
        f"If this Mac is already on the latest release, see {NEWER_STORAGE_URL}."
    )


def _apply_verified_crypto_repair(backend: LocalBackend, fetch: CryptoInitFetch) -> None:
    """Add the shared CLI remedy if newer evidence arrives after verification."""
    try:
        crypto.apply_crypto_init_repair(backend, fetch)
    except NewerFormatError as exc:
        # The exception subject can be a race sentence. The shared remedy
        # always names the file, and the typed cause keeps the race detail.
        raise CryptoError(_newer_format_message(exc.version)) from exc


def _init_crypto_session(
    backend: LocalBackend,
    passphrase: str,
    config: dict,
    *,
    read_only: bool = False,
    pending: list[str] | None = None,
) -> int:
    """Read mm-crypto-init, drift-check against local config, pin process crypto session.

    Called at the top of every crypto-using command (push/pull/status/diff/gc/etc).
    Returns the authoritative argon2_memory_kb from storage — callers should use
    THIS value, not config["crypto"]["argon2_memory_kb"], to avoid silent drift
    between local config and storage.

    Side-effect: if local config's root_salt_fp is missing (e.g. first command
    after init), populate and save it so future commands can drift-check.
    With read_only=True, backfill only in memory and defer shared-storage
    repair; append its description to pending without changing the int return.

    Raises:
      MindMeldError subclasses — caller chooses presentation (_error for
      interactive; stderr print for autopull/autopush).
    """
    fetch = fetch_crypto_init(backend)
    if fetch.newer_version is not None:
        raise CryptoError(_newer_format_message(fetch.newer_version))
    if fetch.status == "missing":
        raise CryptoError(
            f"crypto: mm-crypto-init (the key header) not found in {backend.root}. "
            "Wait for this folder to finish syncing; if the file never arrives, "
            "restore it from another machine. Never reinitialize this storage."
        )
    if fetch.status == "corrupt":
        raise CryptoError(
            f"crypto: mm-crypto-init in {backend.root} is corrupt. Wait for this "
            "folder to finish syncing; if it stays corrupt, restore a valid copy "
            "from another machine and never reinitialize this storage. "
            f"For non-destructive recovery, see {NEWER_STORAGE_URL}."
        )

    assert fetch.root_salt is not None and fetch.argon2_memory_kb is not None
    storage_fp = root_salt_fingerprint(fetch.root_salt)
    local_fp = config.get("crypto", {}).get("root_salt_fp")
    repair_note = ""
    if fetch.repair_plan is not None:
        counts = crypto.crypto_init_repair_counts(fetch)
        notes = []
        if counts["delete"]:
            copies = _count(
                counts["delete"], "identical conflict copy", "identical conflict copies"
            )
            notes.append(
                "The next mm push, pull, autopull or autopush that verifies the passphrase "
                f"will remove {copies} of mm-crypto-init."
            )
        if counts["preserve"]:
            canonical = fetch.repair_plan.canonical
            if canonical is None or canonical.content_hash is None:
                notes.append(
                    "The main mm-crypto-init file is missing or unreadable; "
                    "keep every key-header copy in place."
                )
            else:
                differing = _count(
                    counts["preserve"],
                    "differing or unreadable copy",
                    "differing or unreadable copies",
                )
                stay = "stays" if counts["preserve"] == 1 else "stay"
                notes.append(f"{differing} {stay} in place; mm never moves or rewrites them.")
        repair_note = " ".join(notes)
        if read_only and pending is not None:
            pointer = (
                " Check this device with 'mm status'; to sort them out, "
                f"see {KEY_HEADER_COPIES_URL}"
                if counts["preserve"]
                else ""
            )
            pending.append(repair_note + pointer)

    if local_fp and local_fp != storage_fp:
        # The stop sentence and the pointer come first: _sync_failure cuts the
        # reason at 384 characters, so pending-cleanup text goes last.
        raise CryptoError(
            f"crypto: mm-crypto-init changed since this device was initialized "
            f"(local fp={local_fp}, storage fp={storage_fp}). If the fleet's copies "
            "are still syncing, wait. If the fingerprints still differ, stop: do not "
            "reinitialize or re-key; 'mm status' explains the copies."
            + (
                f" Pending cleanup does not resolve this mismatch. {repair_note}"
                if repair_note
                else ""
            )
            + f" See {KEY_HEADER_COPIES_URL}"
        )

    set_crypto_session(fetch.root_salt, fetch.argon2_memory_kb)
    master_key = load_master_key(passphrase, fetch.root_salt, fetch.argon2_memory_kb)
    assert fetch.keycheck_blob is not None
    verify_passphrase(master_key, fetch.keycheck_blob)
    if not read_only:
        _apply_verified_crypto_repair(backend, fetch)

    # Backfill local config if needed (first command after an upgrade or
    # a previously-uninitialized config). Silent one-time write.
    #
    # In-memory update serves this process's drift-check on later calls.
    # Persist via patch_config_on_disk (re-reads raw TOML and merges only
    # the crypto keys) so `_apply_defaults`' in-memory path canonicalization
    # is NOT written back over the user's hand-edited paths. Without this,
    # a user's `storage.path = "~/Library/..."` silently becomes the resolved
    # absolute form on the first run after upgrade, and symlinks get
    # dereferenced. See Track 2B (v0.8.8) in docs/roadmap-shipped.md.
    if not local_fp:
        crypto_patch = {
            "root_salt_fp": storage_fp,
            "argon2_memory_kb": fetch.argon2_memory_kb,
        }
        config.setdefault("crypto", {}).update(crypto_patch)
        if read_only:
            return fetch.argon2_memory_kb
        try:
            patch_config_on_disk({"crypto": crypto_patch})
        except OSError:
            pass  # non-fatal; drift check just won't fire next run
        except ConfigError as e:
            # ConfigError here signals on-disk TOML became malformed between
            # load_config and this call (concurrent editor crash, disk issue).
            # Per the v0.8.1 visible-failure contract (docs/invariants/sync.md): data-at-risk
            # signals reach stderr even in quiet mode.
            with _pause_sync_progress():
                print(f"mm: warning: backfill skipped — {e}", file=sys.stderr)

    return fetch.argon2_memory_kb


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

    canonical_exists = backend.exists(mkey)
    canonical_vanished = False
    # Reserved-window bytes are unreadable even beside an older sibling.
    # The validator used to return False for them, which hid a newer conflict
    # or let an older copy win. Record every observed byte, including the
    # validator's own read, and refuse before either outcome.
    newer_seen: list[int] = []

    def _note_manifest_bytes(raw: bytes | None) -> None:
        if raw is None:
            return
        version = _newer_version(raw)
        if version is not None:
            newer_seen.append(version)

    def _corrupt_if_newer() -> ManifestFetch | None:
        if not newer_seen:
            return None
        return ManifestFetch(status="corrupt", newer_version=max(newer_seen))

    if canonical_exists:
        try:
            _note_manifest_bytes(backend.get(mkey, expected=True))
        except StorageNotFound:
            canonical_vanished = True
        except (OSError, MindMeldError):
            pass
    for path in backend.find_conflict_copies(mkey, lambda _path: True):
        try:
            _note_manifest_bytes(path.read_bytes())
        except OSError:
            pass
    if (blocked := _corrupt_if_newer()) is not None:
        return blocked

    def _valid_and_current(path: Path) -> bool:
        try:
            raw = path.read_bytes()
        except OSError:
            return False
        # Judge this read. A second read can observe a newer replacement and
        # report it as an ordinary invalid sibling.
        _note_manifest_bytes(raw)
        if _newer_version(raw) is not None or not raw or raw[0] != FORMAT_VERSION:
            return False
        try:
            deserialize_manifest(decrypt(raw, passphrase, memory_kb))
            return True
        except Exception:
            return False

    conflict_copies = backend.find_conflict_copies(mkey, _valid_and_current)
    if (blocked := _corrupt_if_newer()) is not None:
        return blocked

    # Try canonical manifest. Storage-layer errors (OSError, StorageError, any
    # MindMeldError) are treated as "this copy unreadable" — try conflict
    # copies next. Without this, a TOCTOU race between backend.exists() and
    # backend.get(), or a permission flip mid-scan, would escape as an
    # exception and crash recovery.
    if canonical_exists and not canonical_vanished:
        try:
            enc_data = backend.get(mkey, expected=True)
            plain = decrypt(enc_data, passphrase, memory_kb)
            manifests.append(load_manifest(plain))
        except StorageNotFound:
            canonical_vanished = True
        except NewerFormatError as exc:
            # The preliminary scan can miss a copy that becomes newer before
            # this read. Do not fall through to an older sibling.
            return ManifestFetch(status="corrupt", newer_version=exc.version)
        except (CryptoError, ManifestError, OSError, MindMeldError):
            pass  # canonical unreadable — try conflict copies
    # Absent or unreadable canonical: any result comes from conflict copies.
    conflict_copies_only = not manifests
    # Decide absence after the final reads: a source observed during the scan
    # can disappear before decryption. Other read/decrypt errors stay corrupt.
    had_any_source = canonical_exists and not canonical_vanished
    conflict_vanished = False

    # Try conflict copies (iCloud/Dropbox/Syncthing)
    for conflict_path in conflict_copies:
        try:
            enc_data = conflict_path.read_bytes()
            plain = decrypt(enc_data, passphrase, memory_kb)
            manifests.append(load_manifest(plain))
        except FileNotFoundError:
            conflict_vanished = True
        except NewerFormatError as exc:
            return ManifestFetch(status="corrupt", newer_version=exc.version)
        except (CryptoError, ManifestError, OSError, MindMeldError):
            had_any_source = True  # unreadable is distinct from absent

    if not manifests:
        if had_any_source:
            return ManifestFetch(status="corrupt")
        return ManifestFetch(status="missing", vanished=canonical_vanished or conflict_vanished)

    if len(manifests) == 1:
        return ManifestFetch(
            status="ok",
            manifest=manifests[0],
            conflict_copies_only=conflict_copies_only,
            vanished=canonical_vanished,
        )

    # Merge multiple manifests additively.
    #
    # INVARIANT (load-bearing): files are merged as a UNION across conflict
    # copies (see _merge_manifests). Tombstones are newest-timestamp-wins.
    # The asymmetry is correct because the manifest walker is LOSSY — it
    # Diagnostic walkers drop files on permission errors, size-exceeded, and
    # read failures; publishing scans refuse those paths instead. A file
    # missing from the newer conflict copy is NOT causal evidence of deletion;
    # only an explicit tombstone is. Swapping files to newest-wins would silently
    # resurrect-via-erase any file that happened to be locked/unreadable
    # during one scan but not another. The correctness guarantee is:
    # union-for-files + newest-wins-for-tombstones + is_tombstoned() gate
    # at every downstream consumer. See SPEC.md "Merge invariants".
    return ManifestFetch(
        status="ok",
        manifest=_merge_manifests(manifests),
        conflict_copies_only=conflict_copies_only,
        vanished=canonical_vanished,
    )


def _build_exclude_map(
    config: dict,
    sources: list[dict[str, Any]] | None = None,
    *,
    strict: bool = False,
    nested_roots: Mapping[str, Iterable[str]] | None = None,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Map source name -> exclude_patterns and directory-skip prefixes.

    Empty glob entries are pruned. Marker-skip prefixes (directories
    containing `.extend-root` or a browser profile's `Local State`) are
    returned separately so the consumer-boundary filter can prefix-match
    rather than fnmatch. Without that merge, a walker-only skip would emit
    deletion tombstones for previously-synced generated files.

    ``nested_roots`` adds nested-checkout roots the caller already observed
    (pull's and diff's probe of incoming paths); this helper never walks for
    them. Push freezes entries under its walk's roots instead.

    Publishing supplies the already-resolved source list and strict mode
    so this helper does not re-enter a permissive ``get_sources``.
    """
    out: dict[str, list[str]] = {}
    prefixes: dict[str, list[str]] = {}
    resolved = sources if sources is not None else get_sources(config)
    for src in resolved:
        patterns = list(src.get("exclude_patterns") or [])
        if src.get("type") == "muse":
            patterns = [*MUSE_EXCLUDE_PATTERNS, *patterns]
        prefs = marker_skip_globs(src, strict=strict)
        prefs = [*prefs, *sorted((nested_roots or {}).get(src["name"], ()))]
        if patterns:
            out[src["name"]] = patterns
        if prefs:
            prefixes[src["name"]] = prefs
    return out, prefixes


def _build_include_map(
    config: dict,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    """Map source name -> include_patterns allowlist (generic sources).

    Fed to ``_filter_excluded_paths`` alongside the exclude map so narrowing
    a source's allowlist strips de-scoped entries from prior/peer manifests
    at the consumer boundary instead of minting deletion tombstones for
    every file the narrower walk no longer publishes.
    """
    resolved = sources if sources is not None else get_sources(config)
    out: dict[str, list[str]] = {}
    for src in resolved:
        if src.get("type") != "generic":
            continue
        patterns = list(src.get("include_patterns") or [])
        if patterns:
            out[src["name"]] = patterns
    return out


def _filter_excluded_paths(
    manifest: dict,
    exclude_map: dict[str, list[str]],
    skip_prefixes: dict[str, list[str]] | None = None,
    include_map: dict[str, list[str]] | None = None,
) -> dict:
    """Return a shallow copy of `manifest` with excluded source-paths stripped.

    Drops entries from `sources.<name>.files` and `tombstones` whose relative
    path matches any glob in `exclude_map[name]` or — when `include_map[name]`
    is non-empty — matches no allowlist pattern in it, AND any conflict-shaped
    basename, `.extend-root` marker, `.git` segment in any letter case, or `.`
    or empty segment. That is intentionally stricter than the push-side
    ``manifest._is_excluded`` gate, whose `.git/` check is case-sensitive. A
    peer-chosen ``foo.sync-conflict-19700101-000000-deadbeef.md`` must not
    materialize — after the gc bar reads the filename timestamp, that
    string would drive the reap age directly.

    Empty `exclude_map` still strips conflict-shaped names; if none are
    present it returns `manifest` unchanged (load-bearing for hot paths:
    avoid copying large manifests when no source declares excludes).

    Apply at the CONSUMER boundary (_pull_core, _push_core), NOT inside
    `_fetch_remote_manifest`. `mm gc` reads this device's raw manifest via
    `_fetch_remote_manifest` to compute referenced blobs — a filtered manifest
    there would mark live own blobs (which peers still read) as orphans and
    silently delete them (codex-2 #1, the gc-bypass hazard). The filter is a CONSUMER-side fix
    for the tombstone-on-exclude-transition bug, not a fetch-side scrub.

    Tombstone keys are `<source>:<rel_path>` post-Track-1B. Bare-path keys
    (legacy v1 manifests) default to the `claude` source per the same
    promotion rule `normalize_manifest` applies.
    """

    def _conflict_shaped(rel_path: str) -> bool:
        # `.extend-root` and `.git` are local selection markers: a peer-planted
        # one would silently narrow what this Mac syncs. `.git` is found by a
        # case-insensitive lstat on APFS, so match any case. Honest writers
        # never publish a `.git` segment (EXCLUDED matches every path part),
        # nor `.`/empty segments, which would dodge literal prefix matching.
        parts = rel_path.split("/")
        return (
            is_conflict_filename(parts[-1])
            or parts[-1] == MARKER_SKIP_NAME
            or any(part.casefold() == ".git" or part in ("", ".") for part in parts)
        )

    def _excluded(src_name: str, rel_path: str) -> bool:
        if _conflict_shaped(rel_path):
            return True
        if _under_skip_prefix(rel_path, (skip_prefixes or {}).get(src_name)):
            return True
        patterns = exclude_map.get(src_name)
        if patterns and any(fnmatch.fnmatch(rel_path, p) for p in patterns):
            return True
        includes = (include_map or {}).get(src_name)
        return bool(includes) and not matches_include_patterns(rel_path, includes)

    if not exclude_map and not skip_prefixes and not include_map:
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


def _filter_symlinked_paths(
    manifest: dict,
    sources: list[dict[str, Any]],
    *,
    strict: bool = False,
    exempt_roots: Mapping[str, Iterable[str]] | None = None,
) -> dict:
    """Remove locally-symlinked paths from a prior manifest before tombstones.

    Walkers deliberately omit descendant symlinks. Filtering the prior
    manifest at this consumer boundary prevents that omission from minting a
    fleet-wide deletion tombstone, including for pre-migration explicit source
    configurations that do not yet carry the new default exclude globs.
    Applies to every source type (Claude, generic, Grok, Muse). Entries and tombstones under
    ``exempt_roots`` (frozen nested checkouts) are kept without probing.
    """
    exempt = {name: sorted(roots) for name, roots in (exempt_roots or {}).items()}
    source_roots = {
        source["name"]: Path(source["path"]).expanduser()
        for source in sources
        if source.get("name") and source.get("path")
    }
    if not source_roots:
        return manifest

    def _symlinked(source_name: str, rel_path: str) -> bool:
        base_path = source_roots.get(source_name)
        if base_path is None or _under_skip_prefix(rel_path, exempt.get(source_name)):
            return False
        try:
            _validate_rel_path(rel_path, where=f"{source_name}:{rel_path}")
        except ManifestError:
            return False
        return pullplan._has_symlinked_component(
            base_path / rel_path,
            base_path,
            strict=strict,
            source_name=source_name,
        )

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

    Drops `sources.<name>` entries whose source name is in `disabled` or
    `RETIRED_SOURCE_NAMES`.
    Empty `disabled` returns `manifest` unchanged when no retired source is present.

    Apply at the CONSUMER boundary (`_pull_core`). Push uses
    `_filter_unselected_sources` for the same asymmetric contract (drop
    unselected source files, keep tombstones). Do not apply inside
    `_fetch_remote_manifest`. Same hazard as `_filter_excluded_paths`:
    `mm gc` reads this device's raw manifest via that path to compute
    referenced blobs, and a filtered manifest there would mark live own
    blobs as orphans.
    Mirror of the exclude_patterns invariant (docs/invariants/sync.md, 2026-04-24
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
    disabled_set = set(disabled) | _config_module.RETIRED_SOURCE_NAMES
    current = manifest.get("sources", {})
    if not current or not any(name in disabled_set for name in current):
        return manifest

    out = dict(manifest)
    out["sources"] = {name: data for name, data in current.items() if name not in disabled_set}
    # Tombstones intentionally preserved across the disable filter — see
    # the asymmetric-filter rationale in the docstring above.

    return out


def _freeze_nested_checkout_entries(
    local_manifest: dict,
    prior_manifest: dict,
    nested_roots: Mapping[str, Iterable[str]],
    *,
    keep: Callable[[dict], bool] | None = None,
) -> dict[str, set[str]]:
    """Keep this Mac's last published entries under its current nested checkouts.

    A checkout's contents stop publishing, but entries advertised before it
    appeared stay unchanged: no upload, no tombstone, and its prior
    tombstones carry forward. Deleting the checkout later tombstones them like
    any deletion, so peer copies converge instead of returning on pull.
    A tombstoned path is never revived (merged conflict copies can carry both).
    ``keep`` gates recovered entries on a stored blob. Returns the frozen paths
    per source; upload must never re-read them.
    """
    frozen: dict[str, set[str]] = {}
    tombstones = prior_manifest.get("tombstones", {})
    for name, roots in nested_roots.items():
        prefixes = sorted(roots)
        local_src = local_manifest.get("sources", {}).get(name)
        prior_files = prior_manifest.get("sources", {}).get(name, {}).get("files", {})
        if local_src is None or not prefixes:
            continue
        files = local_src["files"]
        for rel_path, info in prior_files.items():
            if (
                rel_path not in files
                and _under_skip_prefix(rel_path, prefixes)
                and not is_tombstoned(name, rel_path, tombstones)
                and (keep is None or keep(info))
            ):
                files[rel_path] = dict(info)
                frozen.setdefault(name, set()).add(rel_path)
    return frozen


def _require_published_source_root(src_name: str, base: Path) -> None:
    """Refuse a missing or inaccessible previously published source root."""
    try:
        base.stat()
    except FileNotFoundError as e:
        raise SnapshotError(
            snapshot_refusal(
                source=src_name,
                problem="is missing after it was previously published",
                cause=os_error_cause(e),
                next_action=(
                    "Restore the source folder, or run "
                    f"'mm disable-source {src_name}' if it should stop syncing."
                ),
            )
        ) from e
    except OSError as e:
        raise SnapshotError(
            snapshot_refusal(
                source=src_name,
                problem="could not be read",
                cause=os_error_cause(e),
                next_action=(
                    "Restore read access, or run "
                    f"'mm disable-source {src_name}' if it should stop syncing."
                ),
            )
        ) from e


def _prove_omitted_paths_absent(
    local_manifest: dict,
    prior_manifest: dict | None,
    sources: list[dict[str, Any]],
    *,
    max_file_size: int,
) -> None:
    """Refuse if an omitted prior path is still present or its root is unreadable.

    ``generate_tombstones`` stays a pure comparison. This bounded probe uses
    the trusted local source map only — never a recovered ``base_path``.
    ENOENT/ENOTDIR below an accessible root is genuine absence. A still-
    present regular file omitted by cap/dedup is a snapshot refusal.
    """
    if prior_manifest is None:
        return
    source_map = {source["name"]: source for source in sources if source.get("name")}
    local_sources = local_manifest.get("sources", {})
    for src_name, remote_src in prior_manifest.get("sources", {}).items():
        # Usage-only hosts can publish an empty source before a configuration
        # root exists. Only prior files require an accessible root for proof.
        if not remote_src.get("files"):
            continue
        src_cfg = source_map.get(src_name)
        if src_cfg is None or not src_cfg.get("path"):
            continue
        base = Path(src_cfg["path"]).expanduser()
        _require_published_source_root(src_name, base)
        local_files = local_sources.get(src_name, {}).get("files", {})
        for rel_path in remote_src.get("files", {}):
            if rel_path in local_files:
                continue
            try:
                _validate_rel_path(rel_path, where=f"{src_name}:{rel_path}")
            except ManifestError:
                continue
            extra = list(src_cfg.get("exclude_patterns") or [])
            if src_cfg.get("type") == "muse":
                extra = [*MUSE_EXCLUDE_PATTERNS, *extra]
            if _manifest_is_excluded(
                rel_path, extra, None, list(src_cfg.get("include_patterns") or []) or None
            ):
                continue
            path = base / rel_path
            try:
                st = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as e:
                if e.errno == errno.ENOTDIR:
                    continue
                raise SnapshotError(
                    snapshot_refusal(
                        source=src_name,
                        rel_path=rel_path,
                        problem="could not be read",
                        cause=os_error_cause(e),
                        next_action="Restore read access, then run mm push.",
                    )
                ) from e
            if stat.S_ISLNK(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_size > max_file_size:
                size_mb = st.st_size / (1024 * 1024)
                raise SnapshotError(
                    snapshot_refusal(
                        source=src_name,
                        rel_path=rel_path,
                        problem=(f"is still present but exceeds max_file_size ({size_mb:.1f}MB)"),
                        next_action=(
                            "Raise sync.max_file_size in config.toml or add a "
                            "precise exclude_patterns glob under this source, then run mm push."
                        ),
                    )
                )
            raise SnapshotError(
                snapshot_refusal(
                    source=src_name,
                    rel_path=rel_path,
                    problem="is still present but was omitted from the snapshot",
                    next_action=(
                        "Remove the extra hard-link alias or restore a unique "
                        "path, then run mm push."
                    ),
                )
            )


def _retain_prior_default_sources(
    resolution: SourceResolution,
    remote_manifest: dict | None,
    config: dict,
) -> SourceResolution:
    """Keep previously published default sources selected on legacy configs.

    Auto-detect only appends gstack/codex/gstack-extend when the root is
    present. If a prior snapshot advertised one of those names and the
    root is now missing, selection would otherwise look like retirement.
    Explicit ``sync.sources`` stays user-curated.
    """
    if resolution.explicit or remote_manifest is None:
        return resolution
    disabled = set(config.get("sync", {}).get("disabled_sources") or [])
    selected = list(resolution.selected)
    selected_names = {src["name"] for src in selected}
    extra = False
    for name in remote_manifest.get("sources", {}):
        if name in selected_names or name in disabled:
            continue
        default = get_default_source(name)
        if default is None:
            continue
        selected.append({**default, "path": str(Path(default["path"]).expanduser())})
        selected_names.add(name)
        extra = True
    if not extra:
        return resolution
    return replace(
        resolution,
        selected=selected,
        available=list(resolution.available),
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
    of the SAME device's manifest (core.py:_fetch_remote_manifest keys the
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


_SYNC_OUTPUT = ContextVar("sync_output", default=None)


@contextmanager
def _pause_sync_progress():
    state = _SYNC_OUTPUT.get()
    progress = state.get("progress") if state is not None else None
    paused = getattr(progress, "paused", None)
    if paused is None:
        yield
        return
    with paused():
        yield


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


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


def _prompt_source_toggle(
    source: dict[str, Any], *, current_state: bool, detected: bool | None = None
) -> bool:
    """One Y/n confirm for a single source.

    Single source of truth for the prompt copy + default-Y/N rule. Used
    by `_prompt_sources` (init flow) and `reconfigure_sources` (eng-review
    D5). `current_state` is the default answer:
      - init: whether the source has a qualifying on-disk tree
      - reconfigure: existing consent, or detected Antigravity on legacy config
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

    Muse detection requires its hardcoded ``memory/`` tree, though its prompt
    still defaults off. Antigravity's default source is also detected by its
    separate local conversations root. Custom paths use their own existence.
    """
    path_str = str(source.get("path", ""))
    if not path_str:
        return False
    root = Path(path_str).expanduser()
    if source.get("type") == "muse" or source.get("name") == "muse":
        return _config_module.muse_memory_dir_exists(root)
    if source.get("name") == "agy" and path_str == _config_module.DEFAULT_AGY_DIR:
        from mind_meld import host_usage as _host_usage

        return root.exists() or _host_usage.agy_sessions_root().exists()
    return root.exists()


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


def _compute_recommended_includes_diff(
    sources: list[dict],
) -> list[tuple[str, list[str], list[str] | None]]:
    """Sources whose default `include_patterns` the config lacks or has outgrown.

    Unlike exclude globs, a partial allowlist is a different allowlist, so
    migration fires only when the key is absent or the present list equals,
    as a set, a previous default in `config.RETIRED_DEFAULT_INCLUDE_PATTERNS`
    that the current default replaced, and then proposes the default list
    wholesale. Any other present list, even an empty one, is the user's
    choice; deleting the key brings the prompt back, mirroring recommended
    excludes.
    Returns [(source_name, default_patterns, replaced)]: `replaced` is None
    for an absent key and the list the config holds today otherwise.
    """
    diffs: list[tuple[str, list[str], list[str] | None]] = []
    for src in sources:
        if src.get("type") != "generic":
            continue
        default = get_default_source(src.get("name", ""))
        if default is None:
            continue
        recommended = default.get("include_patterns") or []
        if not recommended:
            continue
        if "include_patterns" not in src:
            diffs.append((src["name"], list(recommended), None))
            continue
        present = src["include_patterns"]
        if not isinstance(present, list) or not all(isinstance(p, str) for p in present):
            continue
        retired = _config_module.RETIRED_DEFAULT_INCLUDE_PATTERNS.get(src["name"], ())
        if set(present) != set(recommended) and frozenset(present) in retired:
            diffs.append((src["name"], list(recommended), list(present)))
    return diffs


def _config_missing_recommended_excludes(config: dict) -> list[str]:
    """Names of explicit `[[sync.sources]]` entries missing recommended excludes.

    Also covers sources missing a recommended `include_patterns` allowlist:
    one migration signal drives the status breadcrumb, the auto-command
    breadcrumb and the interactive prompt, and `mm migrate-config` applies
    both halves. Returns [] when there's no explicit `sync.sources` array
    (legacy claude_dir-only configs and bare configs use DEFAULT_SOURCES
    verbatim, which already includes the recommended patterns — nothing to
    migrate).
    """
    sources = config.get("sync", {}).get("sources")
    if not sources:
        return []
    names = [name for name, _missing, _current in _compute_recommended_excludes_diff(sources)]
    for name, _patterns, _replaced in _compute_recommended_includes_diff(sources):
        if name not in names:
            names.append(name)
    return names


def _autorun_breadcrumb_path() -> Path:
    """`last-autorun.json` next to the recovery sidecar.

    Resolves `sidecar.SIDECAR_DIR` at call time so tests that monkeypatch
    the source constant get full isolation.
    """
    return sidecar.SIDECAR_DIR / "last-autorun.json"


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


DRY_RUN_COMPLETE = "Dry run complete. Nothing was changed except the local lock file."


DRY_RUN_COMPLETE_LOCK_FREE = "Dry run complete. Nothing was changed."


DRY_RUN_REFUSAL = " Nothing was changed except the local lock file (dry run)."


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


def _incoming_nested_roots(
    sources: list[dict[str, Any]], manifests: Iterable[dict | None]
) -> dict[str, list[str]]:
    """Map source name -> local checkout roots above incoming peer files and tombstones."""
    incoming: dict[str, set[str]] = {}
    for peer in manifests:
        if peer is None:
            continue
        for name, data in peer.get("sources", {}).items():
            incoming.setdefault(name, set()).update(data.get("files", {}))
        for key in peer.get("tombstones", {}):
            if not isinstance(key, str):
                continue
            name, rel = key.split(":", 1) if ":" in key else ("claude", key)
            incoming.setdefault(name, set()).add(rel)
    out: dict[str, list[str]] = {}
    for src in sources:
        if roots := manifest.nested_repo_roots_for_paths(src, incoming.get(src["name"], ())):
            out[src["name"]] = roots
    return out


def _recover_prior_manifest(
    fetch: ManifestFetch,
    backend: LocalBackend,
    device_id: str,
    passphrase: str,
    memory_kb: int,
    *,
    quiet: bool = False,
    dry_run: bool = False,
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
      4. status == "missing" with `vanished` (Track 87A: the manifest was
                                 there and a sync tool is replacing it) →
                                 the same chain as corrupt, because a writer
                                 that read it as a first push would drop this
                                 device's tombstones.

    An "ok" built only from conflict copies may predate this device's latest
    deletions, so it is merged with the sidecar to keep their tombstones.

    Raises typer.Exit(1) via _error() when corrupt + no sidecar + no peers.
    """
    if fetch.is_ok and not fetch.conflict_copies_only:
        return fetch.manifest
    if fetch.is_ok:
        # The returned prior (tombstones, source retention, nested-checkout
        # freeze) gains the last accepted push's records; blob diffs still
        # compare against fetch.manifest alone.
        sidecar_manifest = sidecar.read(device_id)
        rewrite = "mm push would rewrite it" if dry_run else "rewriting it"
        if sidecar_manifest is None:
            msg = (
                "this device's manifest is held only by conflict copies and no usable "
                f"local sidecar exists; {rewrite} from local files with those copies "
                "as the deletion baseline. Recent local deletions may be lost — verify "
                "no files have resurrected."
            )
        else:
            msg = (
                "this device's manifest is held only by conflict copies; "
                f"{rewrite} from local files, keeping deletion records from the "
                "local sidecar."
            )
        with _pause_sync_progress():
            if quiet:
                # Rewriting a manifest from conflict copies is data-at-risk work;
                # autopush must still surface it.
                print(f"mm: warning: {msg}", file=sys.stderr)
            else:
                console.print(f"[yellow]Warning:[/yellow] {msg}")
        if sidecar_manifest is None:
            return fetch.manifest
        return _merge_manifests([fetch.manifest, sidecar_manifest])
    if fetch.status == "missing" and not fetch.vanished:
        return None

    # status == "corrupt", or "missing" because the manifest vanished mid-read.
    what = "corrupt" if fetch.status == "corrupt" else "unreadable (still arriving from sync)"
    sidecar_manifest = sidecar.read(device_id)
    if sidecar_manifest is not None:
        msg = (
            f"remote manifest {what}; recovered prior state from local "
            f"sidecar ({sidecar.sidecar_path()})."
        )
        with _pause_sync_progress():
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
            f"remote manifest {what} and no local sidecar; recovered "
            f"{len(peer_tombstones)} tombstone(s) from peer device(s). "
            "Recent local deletions may be lost — verify no files have "
            "resurrected."
        )
        with _pause_sync_progress():
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
    suffix = " Nothing was changed except the local lock file (dry run)." if dry_run else ""
    remedy = (
        "Run 'mm status' to inspect storage state, then 'mm init' if storage is unrecoverable."
        if fetch.status == "corrupt"
        else "Wait for the sync to finish, then run 'mm push' again."
    )
    _error(
        f"remote manifest {what}, no local sidecar, and no peer manifests "
        f"available for recovery. {remedy} Pushing now would "
        "erase this device's deletion records across your fleet." + suffix
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


def _print_apply_warning(message: str) -> None:
    """A dead stderr must not turn a contained failure into a batch failure."""
    with _pause_sync_progress():
        try:
            print(message, file=sys.stderr)
        except OSError:
            pass


# ── push ──────────────────────────────────────────────────────────────


class _SyncProgress:
    """Transient terminal spinner and counter display for attended push/pull.

    Uses the same Rich columns, 100ms ticker thread, and display-failure
    isolation as `_run_update_with_progress`, with `transient=True` so the
    working line clears before warnings, prompts, or the final outcome line.
    """

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled
        self._lock = threading.RLock()
        self._progress: Progress | None = None
        self._task: Any = None
        self._stop_ticking: threading.Event | None = None
        self._ticker: threading.Thread | None = None
        self._state = updateprogress.UpdateProgress("")
        self._batch_phase = ""
        self._batch_unit = ""
        self._batch_completed = 0
        self._batch_total = 0
        self._pause_depth = 0
        self._stopped = False
        self._last_render_at = 0.0

    def _can_draw(self) -> bool:
        # A closed/replaced stdout makes isatty() raise; that is a display
        # failure, never a sync failure.
        try:
            return (
                self._enabled
                and not self._stopped
                and console.is_terminal
                and console.file.isatty()
            )
        except (Exception, SystemExit):
            return False

    def _render_locked(self, state: updateprogress.UpdateProgress, *, force: bool = False) -> None:
        self._state = state
        if (
            not self._enabled
            or self._progress is None
            or self._task is None
            or self._pause_depth > 0
        ):
            return
        measured = state.total is not None and state.completed is not None
        detail = f"{100 * state.completed / state.total:.1f}% {state.detail}" if measured else ""
        # Per-file advances only update the task fields; the ticker paints
        # them at 10 Hz. A synchronous redraw per file would let a stalled
        # terminal block the sync mid-run, lock held. Forced renders (phase
        # changes, first/final file) paint immediately.
        now = time.monotonic()
        do_refresh = force or now - self._last_render_at >= _UPDATE_SPINNER_TICK_SECONDS
        try:
            self._progress.update(
                self._task,
                completed=0,
                description=state.phase,
                detail=detail.rstrip(),
                refresh=do_refresh,
            )
        except (Exception, SystemExit):
            # Dead terminal: stop retrying the draw on every remaining file.
            self._enabled = False
            return
        if do_refresh:
            self._last_render_at = now

    def start(self, phase: str) -> None:
        with self._lock:
            self._state = updateprogress.UpdateProgress(phase)
            self._batch_total = 0
            if self._progress is not None:
                self._render_locked(self._state, force=True)
                return
            if not self._can_draw():
                return
            progress = Progress(
                SpinnerColumn(),
                TextColumn("{task.description}", markup=False),
                TextColumn("{task.fields[detail]}", markup=False),
                console=console,
                transient=True,
                auto_refresh=False,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            task = progress.add_task(phase, total=1, detail="")
            stop_ticking = threading.Event()

            def tick() -> None:
                while not stop_ticking.wait(_UPDATE_SPINNER_TICK_SECONDS):
                    if self._stopped or self._pause_depth > 0 or self._progress is not progress:
                        continue
                    try:
                        progress.refresh()
                    except (Exception, SystemExit):  # First failed draw: timer off.
                        return

            ticker = threading.Thread(target=tick, name="mm-sync-spinner", daemon=True)
            try:
                progress.start()
            except (Exception, SystemExit):
                with suppress(Exception, SystemExit):
                    progress.stop()
                with suppress(Exception, SystemExit):
                    console.show_cursor(True)  # A failed start can skip Rich's own restore.
                self._enabled = False
                return
            self._progress = progress
            self._last_render_at = time.monotonic()
            self._task = task
            self._stop_ticking = stop_ticking
            self._ticker = ticker
            try:
                with suppress(Exception):  # No ticker thread: state changes still redraw.
                    ticker.start()
            except BaseException:
                self.stop()
                raise

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._batch_total = 0
            self._render_locked(updateprogress.UpdateProgress(phase), force=True)

    def start_batch(self, phase: str, total: int, unit: str) -> None:
        with self._lock:
            self._batch_phase = phase
            self._batch_unit = unit
            self._batch_completed = 0
            self._batch_total = max(total, 0)
            self._render_locked(updateprogress.UpdateProgress(phase), force=True)

    def ensure_batch(self, phase: str, total: int, unit: str) -> None:
        with self._lock:
            if (
                self._batch_total > 0
                and self._batch_completed < self._batch_total
                and self._batch_phase == phase
            ):
                return
            self.start_batch(phase, total, unit)

    def advance(self, step: int = 1) -> None:
        with self._lock:
            if self._batch_total <= 0:
                return
            self._batch_completed = min(self._batch_total, self._batch_completed + step)
            detail = f"{self._batch_completed}/{self._batch_total} {self._batch_unit}".rstrip()
            self._render_locked(
                updateprogress.UpdateProgress(
                    self._batch_phase,
                    completed=self._batch_completed,
                    total=self._batch_total,
                    detail=detail,
                ),
                force=self._batch_completed in (1, self._batch_total),
            )

    @contextmanager
    def paused(self):
        with self._lock:
            self._pause_depth += 1
            progress = self._progress if self._pause_depth == 1 else None
        if progress is not None:
            with suppress(Exception, SystemExit):
                progress.stop()
        propagating = False
        try:
            yield
        except BaseException:
            # Unwinding (e.g. Ctrl-C at a prompt): do not resurrect the
            # spinner on the way out. A pause inside a handled `except:`
            # block is not propagation and still resumes.
            propagating = True
            raise
        finally:
            with self._lock:
                self._pause_depth = max(0, self._pause_depth - 1)
                if (
                    self._pause_depth == 0
                    and self._progress is not None
                    and not self._stopped
                    and not propagating
                    and self._can_draw()
                ):
                    try:
                        self._progress.start()
                        self._render_locked(self._state, force=True)
                    except (Exception, SystemExit):
                        with suppress(Exception, SystemExit):
                            self._progress.stop()
                        with suppress(Exception, SystemExit):
                            console.show_cursor(True)
                        # Match start() failure: drop the dead display so no
                        # later render targets it. Ticker refs stay for stop().
                        self._progress = None
                        self._task = None
                        self._enabled = False

    def stop(self) -> None:
        self._stopped = True
        progress = self._progress
        stop_ticking = self._stop_ticking
        ticker = self._ticker
        self._progress = None
        self._task = None
        self._stop_ticking = None
        self._ticker = None
        if stop_ticking is not None:
            stop_ticking.set()
        if ticker is not None:
            with suppress(Exception, SystemExit):
                if ticker.ident is not None:
                    ticker.join(timeout=_UPDATE_SPINNER_JOIN_SECONDS)
        if progress is not None and (ticker is None or not ticker.is_alive()):
            with suppress(Exception, SystemExit):
                progress.stop()


def _current_sync_progress() -> _SyncProgress | None:
    state = _SYNC_OUTPUT.get()
    if state is None:
        return None
    progress = state.get("progress")
    return progress if isinstance(progress, _SyncProgress) else None


def _start_sync_progress(phase: str) -> None:
    progress = _current_sync_progress()
    if progress is not None:
        progress.start(phase)


def _sync_progress_phase(phase: str) -> None:
    progress = _current_sync_progress()
    if progress is not None:
        progress.set_phase(phase)


def _sync_progress_start_batch(phase: str, total: int, unit: str) -> None:
    progress = _current_sync_progress()
    if progress is not None:
        progress.start_batch(phase, total, unit)


def _sync_progress_ensure_batch(phase: str, total: int, unit: str) -> None:
    progress = _current_sync_progress()
    if progress is not None:
        progress.ensure_batch(phase, total, unit)


def _sync_progress_advance(step: int = 1) -> None:
    progress = _current_sync_progress()
    if progress is not None:
        progress.advance(step)


def _stop_sync_progress() -> None:
    progress = _current_sync_progress()
    if progress is not None:
        progress.stop()


def _sync_diagnostic(message: str, degraded: bool = False) -> None:
    """Retain facts before best-effort rendering; never change execution policy."""
    state = _SYNC_OUTPUT.get()
    if state is not None and degraded:
        state["diagnostics"].append(message.removeprefix("mm: notice: ").strip())
    if state is None or state["verbose"]:
        with suppress(Exception, SystemExit):
            print(safety.safe_terminal_str(message.rstrip()), file=sys.stderr)


def _sync_warning(message: str) -> None:
    with suppress(Exception, SystemExit):
        _print_apply_warning(message)


def _release_sync_lock() -> None:
    if _SYNC_OUTPUT.get() is None:
        release_lock()
        return
    try:
        release_lock()
    except (Exception, SystemExit) as exc:
        _sync_diagnostic("lock release could not be confirmed", True)
        _sync_warning(
            f"mm: warning: lock release could not be confirmed ({type(exc).__name__}); run mm diag"
        )


# ── migrate-config ────────────────────────────────────────────────────


def _explicit_retired_sources(sources: list[dict] | None) -> list[str]:
    """Return explicit [[sync.sources]] names that belong to RETIRED_SOURCE_NAMES.

    Retired names are already ignored during source resolution and at manifest
    consumer boundaries, without deletion tombstones. ``mm migrate-config``
    removes their inert blocks and records their names in ``disabled_sources``.
    """
    if not sources:
        return []
    return [
        s["name"]
        for s in sources
        if isinstance(s.get("name"), str) and s["name"] in _config_module.RETIRED_SOURCE_NAMES
    ]


def _migrate_config_core(*, yes: bool, dry_run: bool) -> bool:
    """Body of `mm migrate-config`. Extracted so the interactive
    `_maybe_prompt_migration` can call it directly without going through
    typer's option-parsing machinery. Returns True only after writing exclusions.
    """
    config = _get_config(read_only=dry_run)

    sources = config.get("sync", {}).get("sources")
    if not sources:
        console.print(
            "[dim]Config has no explicit [[sync.sources]] entries — "
            "nothing to migrate. DEFAULT_SOURCES already include the "
            "recommended excludes.[/dim]"
        )
        if dry_run:
            console.print(DRY_RUN_COMPLETE_LOCK_FREE)
        return False

    diffs = _compute_recommended_excludes_diff(sources)
    include_diffs = _compute_recommended_includes_diff(sources)
    retired = _explicit_retired_sources(sources)
    if not diffs and not include_diffs and not retired:
        console.print("[green]Config is already up to date.[/green]")
        if dry_run:
            console.print(DRY_RUN_COMPLETE_LOCK_FREE)
        return False

    if diffs:
        explanation = "These globs stop per-machine caches and generated output from syncing"
        if any(name == "gstack" for name, _missing, _current in diffs):
            explanation += (
                " (for gstack: virtualenvs, app bundles, archives and binaries, QA evidence)"
            )
        console.print(
            f"\n{explanation}. Pushing publishes the smaller set: that push counts these files "
            "as deleted from this Mac's manifest, writes no tombstones, and every Mac keeps "
            "its copy. Removing a recommended glob later brings this prompt back."
        )
        console.print("\n[bold]Recommended exclude_patterns updates:[/bold]")
        for name, missing, current in diffs:
            console.print(f"\n  [bold]{name}[/bold]  (current: {current!r})")
            for p in missing:
                console.print(f"    [green]+ {p}[/green]")
    if include_diffs:
        console.print(
            "\nThe include_patterns allowlist syncs only context with a cross-machine "
            "reader (checkpoints, plans, learnings/decisions ledgers, handoffs); per-run "
            "output (review-prep trees, loop state, gate diffs) stays on this Mac. "
            "Factory manager files, evidence ledgers, pair-review resume copies and "
            "non-document files in artifact directories stay local too. "
            "Pushing publishes the smaller set with no tombstones, and every Mac keeps "
            "its local files. Deleting the key later brings this prompt back; editing "
            "the list (even to empty) is respected as your choice."
        )
        console.print("\n[bold]Recommended include_patterns updates:[/bold]")
        for name, patterns, replaced in include_diffs:
            if replaced is None:
                console.print(f"\n  [bold]{name}[/bold]  (no include_patterns today)")
                shown_removed: list[str] = []
                shown_added = patterns
            else:
                # Only the difference: the unchanged entries are not news.
                console.print(f"\n  [bold]{name}[/bold]  (previous default list)")
                shown_removed = [p for p in replaced if p not in patterns]
                shown_added = [p for p in patterns if p not in replaced]
            for p in shown_removed:
                console.print(f"    [red]- {p}[/red]")
            for p in shown_added:
                console.print(f"    [green]+ {p}[/green]")
    if retired:
        console.print("\n[bold]Retired sources:[/bold]")
        for rname in retired:
            console.print(
                f"  [bold]{rname}[/bold] — already stopped syncing; remove the leftover "
                "[[sync.sources]] block and record the name in disabled_sources. "
                "Local files are preserved."
            )

    if dry_run:
        console.print(DRY_RUN_COMPLETE_LOCK_FREE)
        return False

    if not yes and not typer.confirm("\nApply these updates?", default=True):
        console.print("[yellow]Cancelled.[/yellow]")
        return False

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    try:
        diff_map = {name: missing for name, missing, _current in diffs}
        include_map_new = {name: patterns for name, patterns, _replaced in include_diffs}
        new_sources: list[dict] = []
        for src in sources:
            if src.get("name") in _config_module.RETIRED_SOURCE_NAMES:
                continue
            if src.get("name") in diff_map or src.get("name") in include_map_new:
                merged = dict(src)
                if src.get("name") in diff_map:
                    current = list(src.get("exclude_patterns") or [])
                    merged["exclude_patterns"] = current + diff_map[src["name"]]
                if src.get("name") in include_map_new:
                    merged["include_patterns"] = include_map_new[src["name"]]
                new_sources.append(merged)
            else:
                new_sources.append(src)

        # One patch for both halves. patch_config_on_disk is section-level
        # shallow and REPLACES sync.sources wholesale; disabled_sources is
        # a sibling field in the same section. Record the retirement in config;
        # consumer filters already suppress retired sources without tombstones.
        sync_updates: dict[str, Any] = {"sources": new_sources}
        if retired:
            disabled = list(config.get("sync", {}).get("disabled_sources", []) or [])
            for rname in retired:
                if rname not in disabled:
                    disabled.append(rname)
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
        if diffs or include_diffs:
            updated = {name for name, _m, _c in diffs} | {name for name, _p, _r in include_diffs}
            parts.append(f"Updated {len(updated)} source(s)")
        if retired:
            parts.append(f"removed retired source block(s): {', '.join(retired)}")
        console.print(
            f"[green]{'; '.join(parts)}.[/green] Config written to {_config_module.CONFIG_PATH}."
        )
    finally:
        release_lock()
    return bool(diffs or include_diffs)


# Rich's Progress default refresh rate; the dots spinner advances by clock,
# so redrawing at this interval animates smoothly. The timer only redraws
# the current state: phases and counters still change solely on installer
# output, never on elapsed time.
_UPDATE_SPINNER_TICK_SECONDS = 0.1


_UPDATE_SPINNER_JOIN_SECONDS = 1.0


def _migration_state_path() -> Path:
    """`migration-state.json` next to the recovery sidecar.

    Records that an auto-command observed missing recommended excludes but
    refused to mutate config (visible-failure contract — auto-commands MUST
    NEVER silently change user config). `mm status` and the interactive
    prompts read this so the signal stays visible until the user runs
    `mm migrate-config`.
    """
    return sidecar.SIDECAR_DIR / "migration-state.json"


def _maybe_prompt_migration(config: dict, *, read_only: bool) -> None:
    """Once-per-invocation interactive prompt for pending config migrations.

    Called from the top of `mm push` / `mm pull` / `mm recapture` ONLY
    (interactive verbs). Auto-commands (autopull/autopush) NEVER prompt and
    NEVER mutate config — they write a `migration-state.json` breadcrumb
    instead and let `mm status` surface the signal. Visible-failure
    contract: silent config mutation in a hook would be exactly the class
    of "wedged sync I never noticed" failure the contract exists to prevent.
    """
    missing = _config_missing_recommended_excludes(config)
    retired_sources = _explicit_retired_sources(config.get("sync", {}).get("sources"))
    if not missing and not retired_sources:
        return
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    if read_only or not is_tty:
        # Skip the confirm prompt for a read-only preview, or when either
        # stream is not a TTY (CI, piped invocation).
        bits: list[str] = []
        if missing:
            bits.append(f"config missing recommended excludes for source(s) {', '.join(missing)}")
        if retired_sources:
            joined_retired = ", ".join(retired_sources)
            bits.append(f"config still has retired [[sync.sources]] block(s): {joined_retired}")
        stderr_console.print(
            f"[yellow]warning:[/yellow] {'; '.join(bits)}. Run "
            f"[bold]mm migrate-config[/bold] to update."
        )
        if read_only and is_tty:
            stderr_console.print(
                "This preview uses your current config. Without --dry-run, this command "
                "first offers to run mm migrate-config (default: yes)."
            )
        return
    if missing:
        console.print(
            f"\n[yellow]Config is missing recommended exclude_patterns for "
            f"source(s):[/yellow] {', '.join(missing)}"
        )
    if retired_sources:
        console.print(
            f"\n[yellow]Retired sync source(s) detected:[/yellow] {', '.join(retired_sources)}"
        )
        console.print(
            "  These sources have already stopped syncing. mm migrate-config removes "
            "their inert blocks and records their names in disabled_sources. "
            "Local files are preserved."
        )
    if typer.confirm("Run 'mm migrate-config' now?", default=True):
        # Call the core directly so typer's option machinery isn't in the
        # way; the inner "Apply these updates?" prompt still confirms
        # before writing.
        _migrate_config_core(yes=False, dry_run=False)
