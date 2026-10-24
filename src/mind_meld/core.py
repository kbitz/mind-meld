"""Shared command setup, manifest comparison and sync-output context."""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import stat
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import typer

from mind_meld import (
    __version__,
    crypto,
    pullplan,
    sidecar,
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
from mind_meld.consoles import stderr_console
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
    NEWER_STORAGE_URL,
    ConfigError,
    CryptoError,
    ManifestError,
    MindMeldError,
    NewerFormatError,
    SnapshotError,
    os_error_cause,
    snapshot_refusal,
)
from mind_meld.manifest import (
    MARKER_SKIP_NAME,
    MUSE_EXCLUDE_PATTERNS,
    DiffResult,
    _under_skip_prefix,
    _validate_rel_path,
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
from mind_meld.storage.keys import (
    manifest_key,
)
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
    """

    status: FetchStatus
    manifest: dict | None = None
    newer_version: int | None = None
    conflict_copies_only: bool = False

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
            "crypto: mm-crypto-init not found at storage root. "
            "Run 'mm init' to initialize this storage."
        )
    if fetch.status == "corrupt":
        raise CryptoError(
            "crypto: mm-crypto-init is corrupt. If another device still has a "
            "valid copy in its local iCloud cache, wait for sync to reconcile. "
            f"For non-destructive recovery, see {NEWER_STORAGE_URL}."
        )

    assert fetch.root_salt is not None and fetch.argon2_memory_kb is not None
    storage_fp = root_salt_fingerprint(fetch.root_salt)
    local_fp = config.get("crypto", {}).get("root_salt_fp")
    repair_note = ""
    if fetch.repair_plan is not None:
        counts = crypto.crypto_init_repair_counts(fetch)
        replacement = "replace the canonical copy; " if counts["replace_canonical"] else ""
        repair_note = (
            "The next mm push, pull, autopull or autopush that verifies the passphrase "
            f"will reconcile mm-crypto-init: {replacement}delete {counts['delete']} identical "
            f"conflict copies; preserve {counts['preserve']} differing or unreadable copies. "
            "This storage is shared by every Mac."
        )
        if read_only and pending is not None:
            pending.append(repair_note)

    if local_fp and local_fp != storage_fp:
        raise CryptoError(
            f"crypto: mm-crypto-init root_salt changed since this device was "
            f"initialized (local fp={local_fp}, storage fp={storage_fp}). "
            f"Another device may have bootstrapped storage concurrently. "
            "Allow the fleet's mm-crypto-init copies to finish syncing, then inspect "
            "with 'mm diag'. If the fingerprints still differ after syncing, stop; "
            "do not reinitialize or re-key."
            + (
                " Pending conflict cleanup does not resolve this device's fingerprint "
                f"mismatch. {repair_note}"
                if repair_note
                else ""
            )
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
            _note_manifest_bytes(backend.get(mkey))
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
        except NewerFormatError as exc:
            # The preliminary scan can miss a copy that becomes newer before
            # this read. Do not fall through to an older sibling.
            return ManifestFetch(status="corrupt", newer_version=exc.version)
        except (CryptoError, ManifestError, OSError, MindMeldError):
            pass  # canonical unreadable — try conflict copies
    # Absent or unreadable canonical: any result comes from conflict copies.
    conflict_copies_only = not manifests

    # Try conflict copies (iCloud/Dropbox)
    for conflict_path in conflict_copies:
        try:
            enc_data = conflict_path.read_bytes()
            plain = decrypt(enc_data, passphrase, memory_kb)
            manifests.append(load_manifest(plain))
        except NewerFormatError as exc:
            return ManifestFetch(status="corrupt", newer_version=exc.version)
        except (CryptoError, ManifestError, OSError, MindMeldError):
            pass  # skip unreadable conflict copies

    if not manifests:
        if had_any_source:
            return ManifestFetch(status="corrupt")
        return ManifestFetch(status="missing")

    if len(manifests) == 1:
        return ManifestFetch(
            status="ok", manifest=manifests[0], conflict_copies_only=conflict_copies_only
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
    containing `.extend-root`) are returned separately so the consumer-
    boundary filter can prefix-match rather than fnmatch. Without that
    merge, a walker-only skip would emit deletion tombstones for
    previously-synced generated files.

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
) -> list[tuple[str, list[str]]]:
    """Sources whose default declares `include_patterns` but config lacks the key.

    Unlike exclude globs, a partial allowlist is a different allowlist, so
    migration only fires when the key is absent entirely and proposes the
    default list wholesale. A user-edited (even empty) list is left alone;
    deleting the key brings the prompt back, mirroring recommended excludes.
    Returns [(source_name, default_patterns)].
    """
    diffs: list[tuple[str, list[str]]] = []
    for src in sources:
        if src.get("type") != "generic" or "include_patterns" in src:
            continue
        default = get_default_source(src.get("name", ""))
        if default is None:
            continue
        recommended = default.get("include_patterns") or []
        if recommended:
            diffs.append((src["name"], list(recommended)))
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
    for name, _patterns in _compute_recommended_includes_diff(sources):
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
