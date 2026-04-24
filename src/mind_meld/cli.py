"""Mind Meld CLI — built with Typer.

Commands: init, push, pull, status, devices, diff, gc, autopull, autopush,
          sources, conflicts, resolve.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import Literal, Optional

import typer
from rich.console import Console
from rich.table import Table

from mind_meld import __version__, fsutil
from mind_meld.config import CONFIG_PATH, DEFAULT_STORAGE_PATH, load_config, save_config, get_sources
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
from mind_meld.devices import _list_devices_impl, register_device, update_last_seen
from mind_meld.errors import CryptoError, LockError, ManifestError, MindMeldError, StorageError
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
    is_tombstoned,
    load_manifest,
    mtime_from_manifest,
    mtime_from_path,
    read_and_hash,
    serialize_manifest,
)
from mind_meld.merge import merge_file, should_merge
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
from mind_meld import sidecar
from mind_meld.synclog import write_sync_log

ApplyOutcome = Literal["written", "merged", "skipped", "conflicted", "unchanged", "failed"]
FetchStatus = Literal["ok", "missing", "corrupt"]
CONFLICT_AGE_DAYS = 30


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
) -> None:
    pass


def _error(msg: str) -> None:
    # Route to stderr so quiet-mode autopush/autopull don't leak error text
    # to stdout (violating the one-line-stderr contract). Interactive users
    # still see the [red]Error:[/red] formatting because terminals render
    # stderr alongside stdout — the separation only matters when stdout is
    # being consumed programmatically.
    stderr_console.print(f"[red]Error:[/red] {msg}")
    raise typer.Exit(1)


def _list_devices_warn(backend) -> list[dict]:
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
            f"[yellow]Warning:[/yellow] dropped device entry {key} — {reason}"
        )
    return _list_devices_impl(backend, on_drop=_warn)


def _get_config() -> dict:
    try:
        return load_config()
    except MindMeldError as e:
        _error(str(e))
        raise  # unreachable, but keeps type checker happy


def _get_passphrase_or_exit() -> str:
    try:
        return get_passphrase()
    except CryptoError as e:
        _error(str(e))
        raise


def _init_crypto_session(backend, passphrase: str, config: dict) -> int:
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
    master_key = load_master_key(
        passphrase, fetch.root_salt, fetch.argon2_memory_kb
    )
    assert fetch.keycheck_blob is not None
    verify_passphrase(master_key, fetch.keycheck_blob)

    # Backfill local config if needed (first command after an upgrade or
    # a previously-uninitialized config). Silent one-time write.
    if not local_fp:
        config.setdefault("crypto", {})["root_salt_fp"] = storage_fp
        config["crypto"]["argon2_memory_kb"] = fetch.argon2_memory_kb
        try:
            save_config(config)
        except OSError:
            pass  # non-fatal; drift check just won't fire next run

    return fetch.argon2_memory_kb


def _make_manifest_validator(
    passphrase: str, memory_kb: int
) -> Callable[[Path], bool]:
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
    backend, device_id: str, passphrase: str, memory_kb: int
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


def _recover_prior_manifest(
    fetch: ManifestFetch,
    backend,
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
    peer_tombstones = _collect_peer_tombstones(
        backend, device_id, passphrase, memory_kb
    )
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
    backend, my_device_id: str, passphrase: str, memory_kb: int
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
            peer_fetch = _fetch_remote_manifest(
                backend, did, passphrase, memory_kb
            )
            peer_manifests[did] = (
                peer_fetch.manifest if peer_fetch.is_ok else None
            )
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
    backend, device_id: str, passphrase: str, memory_kb: int
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
    console.print(f"  [dim]source '{src_name}' ({base_path}):[/dim]")
    for path, info in sorted(diff.new.items()):
        console.print(f"    [green]+ write[/green]    {path}")
    buckets: dict[str, list[str]] = {"merge": [], "skip": [], "conflict": [], "unchanged": []}
    for path, info in diff.modified.items():
        buckets[_predict_pull_outcome(path, info, base_path)].append(path)
    for path in sorted(buckets["merge"]):
        console.print(f"    [cyan]~ merge[/cyan]    {path}")
    for path in sorted(buckets["skip"]):
        console.print(f"    [dim]= skip[/dim]     {path} (local newer)")
    for path in sorted(buckets["conflict"]):
        console.print(f"    [yellow]! conflict[/yellow] {path} (would rename local to .sync-conflict-*)")
    for path in sorted(buckets["unchanged"]):
        console.print(f"    [dim]  unchanged[/dim] {path}")


# ── shared helpers ────────────────────────────────────────────────────


def _upload_changed_blobs(
    backend,
    base_path: Path,
    to_upload: dict[str, dict],
    device_id: str,
    passphrase: str,
    memory_kb: int,
    verbose: bool = False,
) -> int:
    """Upload changed blobs to storage.

    Reads and hashes each file atomically with read_and_hash to avoid
    TOCTOU races. Returns total encrypted bytes transferred.
    """
    bytes_transferred = 0
    for rel_path, info in to_upload.items():
        file_path = base_path / rel_path
        if not file_path.exists():
            if verbose:
                console.print(f"  [dim]skipped (missing): {rel_path}[/dim]")
            continue

        data, _sha = read_and_hash(file_path)
        enc_data = encrypt(data, passphrase, memory_kb)
        bkey = blob_key(device_id, info["sha256"])
        backend.put(bkey, enc_data)
        bytes_transferred += len(enc_data)

        if verbose:
            console.print(f"  [green]\u2191[/green] {rel_path}")

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
) -> str:
    """Prompt interactively for how to handle one conflict. Default keep-both."""
    import difflib

    try:
        local_text = local_path.read_text(errors="replace").splitlines()
    except OSError:
        local_text = ["<unreadable>"]
    remote_text = remote_data.decode("utf-8", errors="replace").splitlines()

    diff = list(difflib.unified_diff(
        local_text, remote_text,
        fromfile=f"local {rel_path}",
        tofile=f"remote {rel_path}",
        lineterm="",
        n=3,
    ))

    console.print(f"\n[bold yellow]Conflict:[/bold yellow] {rel_path}")
    if diff:
        for line in diff[:60]:
            if line.startswith("+") and not line.startswith("+++"):
                console.print(f"  [green]{line}[/green]")
            elif line.startswith("-") and not line.startswith("---"):
                console.print(f"  [red]{line}[/red]")
            else:
                console.print(f"  {line}")
        if len(diff) > 60:
            console.print(f"  [dim]...({len(diff) - 60} more diff lines)[/dim]")
    else:
        console.print("  [dim](files differ but text diff is empty \u2014 likely binary)[/dim]")

    console.print(
        "[bold]Keep which version?[/bold] "
        "(b)oth [default] / (l)ocal / (r)emote / (a)bort pull"
    )
    choice = typer.prompt("Choice", default="b", show_default=False).strip().lower()
    if choice in ("l", "local", "keep-canonical"):
        return "keep-canonical"
    if choice in ("r", "remote", "keep-remote"):
        return "keep-remote"
    if choice in ("a", "abort"):
        return "abort"
    return "keep-both"


# \u2500\u2500 _apply_incoming_file decision tree \u2500\u2500\u2500
#
#   Re-read local state at apply time (user may have edited since _pull_core
#   snapshotted). Decide outcome:
#
#   local missing                           -> WRITE     (remote -> canonical)
#   local hash == remote hash               -> UNCHANGED
#   should_merge(rel_path)                  -> MERGED    (jsonl / MEMORY.md)
#   local mtime > remote mtime              -> SKIPPED   (local newer)
#   local mtime <= remote mtime             -> CONFLICTED
#        rename canonical -> .sync-conflict-<ts>-<device>.<ext>
#        write  remote    -> canonical
#
#   Failures (rename / write) are isolated per-file: the local file is never
#   left destroyed without a recoverable trail. Returns "failed" on error.

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
        console.print(f"  [red]write failed:[/red] {rel_path} \u2014 {e}")
        return "failed"
    if verbose:
        console.print(f"  [green]\u2193[/green] {rel_path}")
    return "written"


def _apply_merge(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    verbose: bool = False,
) -> ApplyOutcome:
    """[M] mergeable: jsonl / MEMORY.md are line-union safe."""
    try:
        local_bytes = local_path.read_bytes()
        merged = merge_file(rel_path, local_bytes, plain_data)
        fsutil.atomic_write_bytes(local_path, merged, fsync=False)
    except (OSError, StorageError) as e:
        console.print(f"  [red]merge failed:[/red] {rel_path} \u2014 {e}")
        return "failed"
    if verbose:
        console.print(f"  [cyan]merged[/cyan] {rel_path}")
    return "merged"


def _apply_conflict(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    remote_device_id: str,
    verbose: bool = False,
) -> ApplyOutcome:
    """[C] conflict path: rename local to .sync-conflict-*, write remote to canonical.

    Rollback on write failure so canonical still points at something.
    """
    try:
        conflict_path = conflict_filename(local_path, remote_device_id)
    except ValueError as e:
        # Empty/None remote_device_id (corrupted peer manifest). Preserve
        # per-file isolation: warn and fail this file only, keep walking.
        # The pull summary's `failed` count surfaces the issue without
        # losing progress on N other peer files.
        console.print(f"  [red]conflict path build failed (local preserved):[/red] {rel_path} \u2014 {e}")
        return "failed"
    try:
        local_path.rename(conflict_path)
    except OSError as e:
        console.print(f"  [red]conflict rename failed (local preserved):[/red] {rel_path} \u2014 {e}")
        return "failed"

    try:
        fsutil.atomic_write_bytes(local_path, plain_data, fsync=False)
    except (OSError, StorageError) as e:
        # Best-effort rollback so canonical still points at something.
        try:
            conflict_path.rename(local_path)
        except OSError:
            pass  # local now lives at conflict_path only \u2014 not lost, just moved
        console.print(f"  [red]canonical write failed after rename:[/red] {rel_path} \u2014 {e}")
        return "failed"

    if verbose:
        console.print(f"  [yellow]conflict:[/yellow] {rel_path} -> {conflict_path.name}")
    return "conflicted"


def _apply_incoming_file(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    remote_info: dict,
    remote_device_id: str,
    interactive_resolve: bool = False,
    verbose: bool = False,
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
        console.print(f"  [yellow]read failed:[/yellow] {rel_path} \u2014 {e}")
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
        console.print(f"  [yellow]mtime parse failed (forcing conflict):[/yellow] {rel_path} \u2014 {e}")
        local_mtime = None
        remote_mtime = None

    if local_mtime is not None and remote_mtime is not None and local_mtime > remote_mtime:
        if verbose:
            console.print(f"  [dim]= {rel_path} (local newer, kept)[/dim]")
        return "skipped"

    # [C] conflict path. Optionally prompt the user; default keep-both.
    if interactive_resolve:
        choice = _prompt_conflict_choice(rel_path, local_path, plain_data)
        if choice == "keep-canonical":
            if verbose:
                console.print(f"  [dim]= {rel_path} (kept canonical by user)[/dim]")
            return "skipped"
        if choice == "keep-remote":
            # User overrode default keep-both by picking remote \u2014 overwrite
            # canonical with remote bytes. Same I/O as _apply_write but with
            # a different verbose string ("remote kept by user").
            try:
                fsutil.atomic_write_bytes(local_path, plain_data, fsync=False)
            except (OSError, StorageError) as e:
                console.print(f"  [red]write failed:[/red] {rel_path} \u2014 {e}")
                return "failed"
            if verbose:
                console.print(f"  [yellow]\u2193[/yellow] {rel_path} (remote kept by user)")
            return "written"
        if choice == "abort":
            raise typer.Abort()
        # choice == "keep-both" -> fall through to _apply_conflict

    return _apply_conflict(
        local_path, rel_path, plain_data, remote_device_id, verbose=verbose
    )


def _download_and_apply(
    backend,
    base_path: Path,
    to_download: dict[str, dict],
    source_device_id: str,
    passphrase: str,
    memory_kb: int,
    interactive_resolve: bool = False,
    verbose: bool = False,
) -> tuple[int, dict[ApplyOutcome, list[str]]]:
    """Download blobs and dispatch each to _apply_incoming_file.

    Returns (encrypted_bytes_transferred, outcomes_by_path).
    outcomes_by_path groups rel_paths by outcome so callers can report
    per-outcome totals and write accurate sync logs.
    """
    bytes_transferred = 0
    outcomes: dict[ApplyOutcome, list[str]] = {
        "written": [],
        "merged": [],
        "skipped": [],
        "conflicted": [],
        "unchanged": [],
        "failed": [],
    }

    for rel_path, info in to_download.items():
        try:
            bkey = blob_key(source_device_id, info.get("sha256", ""))
        except ValueError as e:
            # Malicious or corrupt manifest shipped a sha256 with path
            # separators / parent-dir refs / null bytes / empty. Per-file
            # isolation: fail this file, keep walking — matches the
            # v0.8.1 empty-device_id handling in _apply_conflict.
            console.print(f"  [red]bad blob key (local preserved):[/red] {rel_path} — {e}")
            outcomes["failed"].append(rel_path)
            continue
        try:
            enc_data = backend.get(bkey)
        except MindMeldError:
            if verbose:
                console.print(f"  [yellow]blob missing: {bkey}[/yellow]")
            outcomes["failed"].append(rel_path)
            continue

        try:
            plain_data = decrypt(enc_data, passphrase, memory_kb)
        except CryptoError as e:
            console.print(f"  [red]decrypt failed:[/red] {rel_path} \u2014 {e}")
            outcomes["failed"].append(rel_path)
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
            verbose=verbose,
        )
        outcomes[outcome].append(rel_path)

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
    has_crypto_init: bool       # fetch_crypto_init().status == "ok"
    has_corrupt_crypto_init: bool
    has_any_blobs: bool         # any data/**/*.enc
    has_any_manifests: bool     # any manifests/**/*.enc
    has_any_devices: bool       # devices/ non-empty (weakest signal)


def _probe_storage_occupancy(backend) -> _StorageOccupancy:
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
    if not occupancy.has_crypto_init and (
        occupancy.has_any_blobs or occupancy.has_any_manifests
    ):
        stderr_console.print(
            "[red]DANGER:[/red] mm-crypto-init is missing from storage, but "
            "encrypted blobs/manifests still exist. Initializing now generates "
            "a NEW root_salt \u2014 [bold]every existing blob becomes "
            "unrecoverable[/bold]. If another device still has a working "
            "mm-crypto-init in its iCloud cache, wait for sync to reconcile "
            "and retry init instead."
        )
        typed = typer.prompt(
            'Type "BRICK" (case-sensitive) to confirm and proceed'
        )
        if typed != "BRICK":
            stderr_console.print("[yellow]Aborted.[/yellow] No state changed.")
            raise typer.Exit(1)
        return

    # Orphan case: storage has state and we're about to add a device entry.
    any_storage = (
        occupancy.has_any_blobs
        or occupancy.has_any_manifests
        or occupancy.has_any_devices
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


@app.command()
def init() -> None:
    """Initialize Mind Meld: generate device ID, configure iCloud storage, set passphrase.

    Two-path flow. Storage is probed (mm-crypto-init) BEFORE any local state is
    written or any passphrase is committed to the keyring.

    First-device path (mm-crypto-init missing):
        Double-prompt passphrase, generate root_salt + keycheck, atomic write,
        then save config + register device + store passphrase.

    Second-device path (mm-crypto-init ok):
        Single-prompt passphrase, derive master_key from the stored root_salt,
        verify keycheck. On success, save config + register + store passphrase.
        On failure, nothing local is written.

    Two-tier re-init guard (Group 2 pre-flight 3):
        * Orphan case \u2014 mm-crypto-init ok + other occupancy: warn that
          a new device entry gets created alongside existing devices;
          require typer.confirm.
        * BRICK case \u2014 mm-crypto-init missing + blobs/manifests exist:
          re-bootstrap would generate a new root_salt and brick every
          existing blob. Refuse by default; require exact typed "BRICK".
    """
    # Capture existing local-device metadata BEFORE any prompt, so the
    # orphan-case warning can name the device that's about to be left
    # behind. Best-effort \u2014 malformed config just loses the name.
    existing_device_id: str | None = None
    existing_device_name: str | None = None
    if CONFIG_PATH.exists():
        try:
            _prior = load_config()
            existing_device_id = _prior.get("device", {}).get("id")
            existing_device_name = _prior.get("device", {}).get("name")
        except MindMeldError:
            pass  # prior config unreadable \u2014 best-effort None
        overwrite = typer.confirm(
            f"Config already exists at {CONFIG_PATH}. Overwrite?"
        )
        if not overwrite:
            raise typer.Exit()

    console.print(f"[bold]Mind Meld v{__version__} \u2014 init[/bold]\n")

    # Storage path first: we need a backend to probe mm-crypto-init.
    storage_path = typer.prompt("Storage folder path", default=DEFAULT_STORAGE_PATH)
    full_path = Path(storage_path).expanduser()
    full_path.mkdir(parents=True, exist_ok=True)
    console.print(f"  Storage: {full_path}")

    # Lightweight backend (config is not yet written; instantiate directly).
    from mind_meld.storage.local import LocalBackend
    from mind_meld.errors import StorageError as _StorageError
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

    # Device metadata.
    device_id = uuid.uuid4().hex[:8]
    device_name = typer.prompt("Device name", default=_default_device_name())

    # Passphrase prompt. Double-prompt when setting a new secret (first-device),
    # single-prompt when entering an existing secret we can verify (second-device).
    if is_first_device:
        passphrase = typer.prompt("Encryption passphrase", hide_input=True)
        if not passphrase:
            _error("Passphrase cannot be empty.")
        passphrase_confirm = typer.prompt("Confirm passphrase", hide_input=True)
        if passphrase != passphrase_confirm:
            _error("Passphrases don't match.")
    else:
        passphrase = typer.prompt("Encryption passphrase", hide_input=True)
        if not passphrase:
            _error("Passphrase cannot be empty.")

    # Bootstrap or verify.
    argon2_memory_kb: int
    root_salt: bytes
    keycheck_blob: bytes

    if is_first_device:
        try:
            bootstrap = bootstrap_crypto_init(
                backend, passphrase, argon2_memory_kb=65_536
            )
        except _StorageError:
            # Race: another device wrote mm-crypto-init between our fetch and
            # our put. Fall through to second-device verify with the winner's blob.
            console.print(
                "  [dim]Another device bootstrapped concurrently \u2014 "
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
        else:
            assert bootstrap.root_salt is not None
            assert bootstrap.argon2_memory_kb is not None
            assert bootstrap.keycheck_blob is not None
            root_salt = bootstrap.root_salt
            argon2_memory_kb = bootstrap.argon2_memory_kb
            keycheck_blob = bootstrap.keycheck_blob
            set_crypto_session(root_salt, argon2_memory_kb)
            console.print(
                f"  mm-crypto-init bootstrapped "
                f"(root_salt fp={root_salt_fingerprint(root_salt)})."
            )
    else:
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

    # All crypto passed; it's safe to write local state.
    config: dict = {
        "device": {"id": device_id, "name": device_name},
        "storage": {"path": storage_path},
        "sync": {"claude_dir": "~/.claude", "max_file_size": 52_428_800},
        "crypto": {
            "argon2_memory_kb": argon2_memory_kb,
            "root_salt_fp": root_salt_fingerprint(root_salt),
        },
    }

    # Check for gstack and offer to add as sync source.
    gstack_path = Path.home() / ".gstack"
    if gstack_path.exists():
        add_gstack = typer.confirm(
            f"gstack detected at {gstack_path}/. Add as sync source?",
            default=True,
        )
        if add_gstack:
            claude_path = config.get("sync", {}).get("claude_dir", "~/.claude")
            config.setdefault("sync", {})["sources"] = [
                {"name": "claude", "path": claude_path, "type": "claude"},
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

    save_config(config)
    console.print(f"  Config written to {CONFIG_PATH}")

    # Register device AFTER config write AND after crypto validation.
    register_device(backend, device_id, device_name)
    console.print(f"  Device registered: {device_name} ({device_id})")

    # Store passphrase in keyring LAST. Gated on crypto validation so a typo'd
    # passphrase on the second-device path never lands in Keychain.
    if store_passphrase_in_keyring(passphrase):
        console.print("  Passphrase stored in OS keyring.")
    else:
        console.print(
            "  [yellow]No keyring available.[/yellow] "
            "Set MINDMELD_PASSPHRASE environment variable instead."
        )

    console.print("\n[green]Mind Meld initialized. Run \'mm push\' to sync.[/green]")


# ── push ──────────────────────────────────────────────────────────────


@app.command()
def push(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed output"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change"),
) -> None:
    """Push local session data to storage."""
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

    skipped: list[tuple[str, str]] = []

    def on_skip(path: str, reason: str) -> None:
        skipped.append((path, reason))
        if verbose and not quiet:
            console.print(f"  [dim]skipped: {path} ({reason})[/dim]")

    if not quiet:
        console.print("[bold]Building manifest...[/bold]")
    local_manifest = build_manifest_v2(
        device_id, device_name, sources, max_file_size, on_skip
    )

    total_file_count = sum(
        len(src_data["files"]) for src_data in local_manifest["sources"].values()
    )
    if not quiet:
        console.print(f"  {total_file_count} files scanned across {len(local_manifest['sources'])} source(s)")
        if skipped:
            console.print(f"  [yellow]{len(skipped)} files skipped[/yellow]")

    # Fetch remote manifest (tri-state: ok / missing / corrupt).
    # `fetch.manifest` (when ok) is pre-normalized via load_manifest;
    # _recover_prior_manifest's sidecar/peer paths emit the same shape.
    fetch = _fetch_remote_manifest(backend, device_id, passphrase, memory_kb)
    remote_manifest = _recover_prior_manifest(
        fetch, backend, device_id, passphrase, memory_kb, quiet=quiet
    )

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
            backend, base_path, to_upload, device_id, passphrase, memory_kb,
            verbose=(verbose and not quiet),
        )
        total_new += len(diff.new)
        total_modified += len(diff.modified)
        total_deleted += len(diff.deleted)

    if dry_run:
        if not quiet:
            elapsed = time.time() - start
            console.print(f"\n[bold]Dry run complete.[/bold]")
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
            console.print(
                "\n[bold]Rewriting manifest to heal remote corruption...[/bold]"
            )
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
        console.print(f"\n[bold green]Push complete.[/bold green]")
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
    from_device: Optional[str] = typer.Option(
        None, "--from", help="Pull from a specific device ID"
    ),
    source: Optional[str] = typer.Option(
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
            config, passphrase, memory_kb, from_device, source, verbose, dry_run,
            conflict_mode=conflict_mode,
        )
    finally:
        release_lock()


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
    # Set for src_name=="claude" to trigger write_sync_log in the caller.
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
        return any(
            self.outcomes[k] for k in self.outcomes if k != "unchanged"
        )


def _select_devices(
    backend, my_device_id: str, from_device: str | None
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
    backend, devices: list[dict], passphrase: str, memory_kb: int
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
            corrupt.append(
                _CorruptPeer(device_id=did, device_name=d.get("device_name", did))
            )
        cache[did] = peer_fetch.manifest if peer_fetch.is_ok else None
    return cache, corrupt


def _preflight_conflicts(
    pull_targets: list[dict],
    manifest_cache: dict[str, dict | None],
    local_sources_map: dict[str, Path],
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
            base_path = local_sources_map[src_name]
            for rel_path, info in src_data.get("files", {}).items():
                if is_tombstoned(src_name, rel_path, all_tombstones):
                    continue
                overlay_sha = overlay.get((src_name, rel_path))
                if overlay_sha is not None:
                    # An earlier peer already predicted a clean write.
                    # Next peer conflicts iff its sha differs from what
                    # the earlier peer will leave.
                    if overlay_sha != info.get("sha256"):
                        predicted.append(
                            _PredictedConflict(dname, src_name, rel_path)
                        )
                    continue
                outcome = _predict_pull_outcome(rel_path, info, base_path)
                if outcome == "conflict":
                    predicted.append(
                        _PredictedConflict(dname, src_name, rel_path)
                    )
                elif outcome in ("write", "merge"):
                    overlay[(src_name, rel_path)] = info.get("sha256", "")
    return predicted


def _empty_outcomes() -> dict[ApplyOutcome, list[str]]:
    return {
        "written": [],
        "merged": [],
        "skipped": [],
        "conflicted": [],
        "unchanged": [],
        "failed": [],
    }


def _pull_one_source(
    backend,
    *,
    src_name: str,
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
) -> _PerSourceResult:
    """Pull one source from one peer. Returns _PerSourceResult.

    `verbose_console` is `(verbose and not quiet)` — controls per-file
    console output inside _download_and_apply.
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
    )

    touched_parents: set[Path] = set()
    for rel in outcomes["written"] + outcomes["merged"] + outcomes["conflicted"]:
        touched_parents.add((base_path / rel).parent)

    return _PerSourceResult(
        src_name=src_name,
        device_name=dname,
        device_id=did,
        outcomes=outcomes,
        bytes_transferred=bt,
        touched_parents=touched_parents,
        claude_sync_base=str(base_path) if src_name == "claude" else None,
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


def _print_preflight_conflicts(
    predicted: list[_PredictedConflict], quiet: bool
) -> None:
    """Print predicted conflicts before --conflict-mode=fail raises.

    Quiet (autopull): one-liner per conflict to stderr.
    Non-quiet: rich console with resolution hint.
    """
    if quiet:
        for p in predicted:
            print(
                f"mm: conflict {p.src_name}/{p.rel_path} (from {p.device_name})",
                file=sys.stderr,
            )
        return
    console.print(
        f"[red]Pull refused:[/red] {len(predicted)} file(s) would conflict."
    )
    for p in predicted:
        console.print(
            f"  [yellow]! conflict[/yellow] {p.src_name}/{p.rel_path} "
            f"(from {p.device_name})"
        )
    console.print(
        "\nResolve conflicts locally, or re-run with "
        "--conflict-mode keep-both to auto-rename."
    )


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
    # as successful pull).
    for peer in corrupt_peers:
        msg = (
            f"manifest for device {peer.device_name} ({peer.device_id}) "
            f"is corrupt - skipping pull from this device."
        )
        if quiet:
            print(f"mm: warning: {msg}", file=sys.stderr)
        else:
            console.print(f"[yellow]Warning:[/yellow] {msg}")

    # Load-bearing: unknown sources (partition risk — rename drift or
    # missed config migration).
    for unk in unknown_sources:
        msg = (
            f"skipping unknown source '{unk.src_name}' from "
            f"{unk.device_name} - not configured locally"
        )
        if quiet:
            print(f"mm: warning: {msg}", file=sys.stderr)
        else:
            console.print(f"  [yellow]Warning:[/yellow] {msg}")

    # Load-bearing: fsync failures (pulls non-durable).
    for w in fsync_warnings:
        msg = f"durability fsync failed on {w.parent_dir} — {w.error}"
        if quiet:
            print(f"mm: warning: {msg}", file=sys.stderr)
        else:
            console.print(f"  [yellow]warning:[/yellow] {msg}")

    if quiet:
        return

    # Per-source lines (conflicts/failures always; verbose otherwise).
    for r in per_source_results:
        src_written = len(r.outcomes["written"])
        src_merged = len(r.outcomes["merged"])
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

    local_sources_map: dict[str, Path] = {
        src_cfg["name"]: Path(src_cfg["path"]).expanduser().resolve()
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

    manifest_cache, corrupt_peers = _prefetch_manifests(
        backend, all_devices, passphrase, memory_kb
    )

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
                console.print(f"\n[bold]Pulling from {dname} ({did})...[/bold]")

            remote_manifest = manifest_cache.get(did)
            if remote_manifest is None:
                if not quiet:
                    console.print(f"  [yellow]No manifest for {dname}[/yellow]")
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

                base_path = local_sources_map[src_name]
                if verbose and not quiet:
                    console.print(f"  [bold]Source '{src_name}' ({base_path}):[/bold]")

                per_source = _pull_one_source(
                    backend,
                    src_name=src_name,
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
                )

                if dry_run and per_source.dry_run_diff is not None:
                    if not quiet:
                        console.print(f"  Dry run for {dname}/{src_name}:")
                        _print_pull_prediction(
                            per_source.dry_run_diff, base_path, src_name
                        )
                    continue

                if not per_source.had_changes:
                    if verbose and not quiet:
                        console.print(f"  [green]Up to date with {dname}/{src_name}.[/green]")
                    continue

                per_source_results.append(per_source)
                bytes_transferred += per_source.bytes_transferred
                touched_parents |= per_source.touched_parents
                total_written += len(per_source.outcomes["written"])
                total_merged += len(per_source.outcomes["merged"])
                total_skipped += len(per_source.outcomes["skipped"])
                total_conflicted += len(per_source.outcomes["conflicted"])
                total_failed += len(per_source.outcomes["failed"])
                device_had_changes = True

                # Claude sync log is best-effort: log file is cosmetic
                # signal for Claude Code, losing it on error is harmless.
                # Swallowing the exception here protects the accumulated
                # corrupt-peer / unknown-source warnings from being lost
                # if write_sync_log raises.
                if per_source.claude_sync_base is not None:
                    try:
                        logs = write_sync_log(
                            claude_dir=per_source.claude_sync_base,
                            device_name=dname,
                            device_id=did,
                            new_files=per_source.outcomes["written"],
                            modified_files=per_source.outcomes["merged"],
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
    source: Optional[str] = typer.Option(
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
    local_manifest = build_manifest_v2(
        device_id, device_name, sources_configs, max_file_size
    )

    # Fetch remote manifest (tri-state — surface missing/corrupt to user).
    # fetch.manifest is pre-normalized via load_manifest.
    fetch = _fetch_remote_manifest(backend, device_id, passphrase, memory_kb)
    remote_manifest = fetch.manifest if fetch.is_ok else None

    remote_sources = remote_manifest.get("sources", {}) if remote_manifest else {}

    # Devices
    devices = _list_devices_warn(backend)

    console.print(f"\n[bold]Mind Meld Status[/bold]")
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
        console.print(
            "  [dim]Remote manifest: not yet pushed from this device.[/dim]"
        )
    elif fetch.status == "corrupt":
        console.print(
            "  [yellow]Remote manifest: CORRUPT[/yellow] — next 'mm push' "
            "will attempt recovery from sidecar or peers."
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
            console.print(f"    [yellow]Pending push:[/yellow]")
            if diff.new:
                console.print(f"      + {len(diff.new)} new")
            if diff.modified:
                console.print(f"      ~ {len(diff.modified)} modified")
            if diff.deleted:
                console.print(f"      - {len(diff.deleted)} deleted")
        else:
            console.print(f"    [green]In sync.[/green]")

    if not source:
        console.print(f"\n  Total local: {total_local}, Total remote: {total_remote}")

    if total_new or total_modified or total_deleted:
        console.print(f"\n  [yellow]Overall pending push:[/yellow]")
        console.print(f"    + {total_new} new, ~ {total_modified} modified, - {total_deleted} deleted")
    elif not source:
        console.print(f"\n  [green]All sources in sync.[/green]")

    if len(devices) > 1:
        console.print(f"\n  Other devices:")
        for d in devices:
            if d["device_id"] != device_id:
                console.print(f"    {d['device_name']} ({d['device_id']})")


# ── diag ──────────────────────────────────────────────────────────────


def _collect_diag_state(backend) -> dict:
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
                backend.find_conflict_copies(
                    manifest_key(dev_id), lambda p: True
                )
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
            "ok" if (local_fp and crypto_init.get("root_salt_fp") == local_fp)
            else "mismatch" if (local_fp and crypto_init.get("status") == "ok")
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
    from mind_meld.storage.local import LocalBackend
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
        console.print(f"  [yellow]breadcrumb unreadable[/yellow]")
    else:
        console.print(f"  verb:       {br.get('verb')}")
        console.print(f"  outcome:    {br.get('outcome')}")
        console.print(f"  timestamp:  {br.get('timestamp')}")
        if br.get("detail"):
            console.print(f"  detail:     {br.get('detail')}")


# ── devices ───────────────────────────────────────────────────────────


@app.command()
def devices() -> None:
    """List all registered devices."""
    config = _get_config()
    backend = get_backend(config)
    device_list = _list_devices_warn(backend)
    my_id = config["device"]["id"]

    if not device_list:
        console.print("[yellow]No devices registered.[/yellow]")
        return

    table = Table(title="Registered Devices")
    table.add_column("Name")
    table.add_column("ID")
    table.add_column("Last Push")
    table.add_column("")

    for d in device_list:
        marker = "[green]\u2190 this device[/green]" if d["device_id"] == my_id else ""
        # `last_seen` is seeded only on push (not at register time), so a
        # registered-but-never-pushed device renders as an em-dash rather
        # than misleadingly showing its registration time.
        table.add_row(
            d.get("device_name", "?"),
            d["device_id"],
            d.get("last_seen", "\u2014"),
            marker,
        )

    console.print(table)


# ── diff ──────────────────────────────────────────────────────────────


@app.command(name="diff")
def diff_cmd(
    from_device: Optional[str] = typer.Option(
        None, "--from", help="Diff against a specific device"
    ),
    source: Optional[str] = typer.Option(
        None, "--source", help="Diff a specific source only"
    ),
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
    local_manifest = build_manifest_v2(
        device_id, device_name, sources_configs, max_file_size
    )

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

    console.print(f"\n[bold]Diff against {'device ' + target_id if from_device else 'remote'}:[/bold]")

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
        console.print(f"\n  [bold]Source '{src_name}':[/bold]")
        console.print(f"  [dim](push direction: local → remote)[/dim]")
        for path in sorted(diff.new):
            console.print(f"    [green]+ push  [/green] {path}")
        for path in sorted(diff.modified):
            # Predict what pulling the REMOTE version would do to local. The
            # manifests were built from the same state, so local hash matches
            # src_data; remote hash is the divergent one.
            remote_info = remote_files.get(path, {})
            base_path = src_base_paths.get(src_name)
            if base_path is not None and remote_info:
                outcome = _predict_pull_outcome(path, remote_info, base_path)
                console.print(f"    [yellow]~ push  [/yellow] {path} (pull would: {outcome})")
            else:
                console.print(f"    [yellow]~ push  [/yellow] {path}")
        for path in sorted(diff.deleted):
            console.print(f"    [red]- only-remote[/red] {path}")

    if not any_changes:
        console.print("[green]No differences.[/green]")


# ── gc ────────────────────────────────────────────────────────────────


@app.command()
def gc(
    dry_run: bool = typer.Option(False, "--dry-run", help="List orphans without deleting"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    prune_conflicts: bool = typer.Option(
        False, "--conflicts",
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
        if prune_conflicts:
            _gc_old_conflict_files(config, dry_run, verbose)
    finally:
        release_lock()


def _sweep_local_tmp_files(
    backend,
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
            f"\n[bold green]GC complete.[/bold green] "
            f"Deleted {orphan_count} orphaned blobs."
        )

    return orphan_count


# ── sources ───────────────────────────────────────────────────────────


@app.command()
def sources() -> None:
    """List configured sync sources."""
    config = _get_config()

    from mind_meld.manifest import walk_source

    src_list = get_sources(config)
    max_file_size = config["sync"]["max_file_size"]

    table = Table(title="Configured Sources")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Type")
    table.add_column("Files")

    for src in src_list:
        base_path, files = walk_source(src, max_file_size)
        table.add_row(src["name"], src["path"], src["type"], str(len(files)))

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


def _find_conflict_files(config: dict) -> list[tuple[str, Path, Path | None]]:
    """Walk all sync sources looking for .sync-conflict-* files.

    Scoped to the same paths mm push walks — won't surface conflict files
    from unsynced areas of the source tree. Returns (source_name,
    conflict_path, canonical_path_if_exists). Canonical is None if the user
    has already deleted it.
    """
    hits: list[tuple[str, Path, Path | None]] = []
    for src_cfg in get_sources(config):
        base_path = Path(src_cfg["path"]).expanduser().resolve()
        if not base_path.exists():
            continue
        for scan_dir in _synced_scan_dirs(src_cfg, base_path):
            # rglob is loose (substring); filter strictly via is_conflict_filename
            # so user files like notes.sync-conflict-log.md are not listed/reaped.
            for conflict_path in scan_dir.rglob(f"*{CONFLICT_INFIX}*"):
                if not conflict_path.is_file():
                    continue
                if not is_conflict_filename(conflict_path.name):
                    continue
                canonical = _canonical_for_conflict(conflict_path)
                hits.append((src_cfg["name"], conflict_path, canonical if canonical.exists() else None))
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
    after = name[idx + len(CONFLICT_INFIX):]
    suffix = ""
    if "." in after:
        suffix = "." + after.rsplit(".", 1)[-1]
    return conflict_path.with_name(before + suffix)


@app.command()
def conflicts() -> None:
    """List .sync-conflict-* files across all synced sources."""
    config = _get_config()
    hits = _find_conflict_files(config)
    if not hits:
        console.print("[green]No conflict files.[/green]")
        return

    table = Table(title=f"Conflict files ({len(hits)})")
    table.add_column("Source")
    table.add_column("Conflict")
    table.add_column("Canonical")
    table.add_column("Age")
    now = datetime.now(timezone.utc)
    for src_name, cpath, canonical in sorted(hits, key=lambda h: str(h[1])):
        try:
            mtime = datetime.fromtimestamp(cpath.stat().st_mtime, tz=timezone.utc)
            age = now - mtime
            age_str = f"{age.days}d" if age.days else f"{age.seconds // 3600}h"
        except OSError:
            age_str = "?"
        canonical_display = str(canonical) if canonical else "[dim](gone)[/dim]"
        table.add_row(src_name, str(cpath), canonical_display, age_str)
    console.print(table)
    console.print(
        "\nRun [bold]mm resolve[/bold] to pick a winner interactively, or "
        "delete files manually with [bold]rm[/bold]."
    )


# ── recover ───────────────────────────────────────────────────────────


def _quarantine_corrupt_manifest(
    backend,
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
    peer_tombstones = _collect_peer_tombstones(
        backend, device_id, passphrase, memory_kb
    )
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
        typed = typer.prompt(
            'Type "RESET" (case-sensitive) to confirm and proceed'
        )
        if typed != "RESET":
            stderr_console.print("[yellow]Aborted.[/yellow] Nothing changed.")
            raise typer.Exit(1)

    try:
        quarantine_path = _quarantine_corrupt_manifest(
            backend, storage_root, device_id
        )
    except FileNotFoundError:
        _error(
            f"{manifest_key(device_id)} not found on disk. "
            f"Nothing to quarantine."
        )
    except OSError as e:
        _error(f"quarantine failed: {e}")

    console.print(
        f"[green]Quarantined[/green] corrupt manifest to "
        f"[dim]{quarantine_path}[/dim]."
    )
    console.print(
        "Next 'mm push' will start fresh with no prior-state manifest. "
        "The quarantined copy is preserved for post-mortem and can be "
        "deleted manually once you've confirmed recovery."
    )


@app.command()
def resolve(
    path: Optional[str] = typer.Argument(
        None,
        help="Specific conflict path to resolve. If omitted, walks all conflicts.",
    ),
) -> None:
    """Interactively resolve .sync-conflict-* files.

    For each conflict: shows a unified diff of canonical vs conflict, then
    prompts: keep canonical / keep conflict / keep both (no-op) / abort.

    "Keep canonical" deletes the conflict file.
    "Keep conflict" renames conflict → canonical (overwriting canonical).
    "Keep both" leaves both files in place.

    Both deletions and renames propagate on the next `mm push` via the
    existing tombstone / additive-sync machinery.

    Acquires the mm lockfile so an autopull running in parallel can't
    race with our rename/unlink operations on the synced files.
    """
    config = _get_config()

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    failed = 0
    try:
        hits = _find_conflict_files(config)

        if path:
            target = Path(path).expanduser().resolve()
            hits = [h for h in hits if h[1] == target]
            if not hits:
                _error(f"No conflict file matching: {path}")

        if not hits:
            console.print("[green]No conflict files.[/green]")
            return
        _, failed = _resolve_interactive_loop(hits)
    finally:
        release_lock()

    if failed:
        # Surface partial-failure as a non-zero exit so CI / scripts driving
        # `mm resolve` can detect that some conflicts were not actually
        # resolved (rename/unlink/read errors mid-walk). Walk continues
        # through every conflict; only the exit code reflects the failure.
        raise typer.Exit(1)


def _resolve_interactive_loop(hits: list[tuple[str, Path, Path | None]]) -> tuple[int, int]:
    """Walk each conflict and prompt for resolution. Extracted so `resolve`
    stays a thin wrapper around acquire/release lock boilerplate.

    Returns (resolved, failed). `failed` covers per-conflict OSErrors
    (rename/unlink/read) that left the conflict file in place. `resolve`
    uses the failure count to decide its exit code; the walk itself does
    not abort on per-file errors (so the user gets to triage every conflict
    in one pass).
    """
    import difflib

    resolved = 0
    failed = 0
    for src_name, cpath, canonical in hits:
        console.print(f"\n[bold yellow]Conflict in {src_name}:[/bold yellow] {cpath}")

        if canonical is None:
            console.print(
                "  [dim]Canonical version no longer exists. "
                "Promote conflict to canonical or delete it?[/dim]"
            )
            choice = typer.prompt(
                "  (p)romote / (d)elete / (s)kip",
                default="s",
                show_default=False,
            ).strip().lower()
            if choice.startswith("p"):
                target_canonical = _canonical_for_conflict(cpath)
                try:
                    cpath.rename(target_canonical)
                    console.print(f"  [green]promoted[/green] {cpath.name} -> {target_canonical.name}")
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]promote failed:[/red] {e}")
                    failed += 1
            elif choice.startswith("d"):
                try:
                    cpath.unlink()
                    console.print(f"  [red]deleted[/red] {cpath.name}")
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]delete failed:[/red] {e}")
                    failed += 1
            continue

        try:
            local_text = canonical.read_text(errors="replace").splitlines()
            conflict_text = cpath.read_text(errors="replace").splitlines()
        except OSError as e:
            console.print(f"  [red]read failed:[/red] {e}")
            failed += 1
            continue

        diff = list(difflib.unified_diff(
            local_text, conflict_text,
            fromfile=f"canonical {canonical.name}",
            tofile=f"conflict  {cpath.name}",
            lineterm="",
            n=3,
        ))
        if diff:
            for line in diff[:80]:
                if line.startswith("+") and not line.startswith("+++"):
                    console.print(f"  [green]{line}[/green]")
                elif line.startswith("-") and not line.startswith("---"):
                    console.print(f"  [red]{line}[/red]")
                else:
                    console.print(f"  {line}")
            if len(diff) > 80:
                console.print(f"  [dim]...({len(diff) - 80} more diff lines)[/dim]")
        else:
            console.print("  [dim](files differ but text diff is empty — likely binary)[/dim]")

        console.print(
            "  [bold]Keep which?[/bold] "
            "(c)anonical / (f)orce conflict -> canonical / (b)oth [default] / (a)bort"
        )
        choice = typer.prompt("  Choice", default="b", show_default=False).strip().lower()
        if choice.startswith("c"):
            try:
                cpath.unlink()
                console.print(f"  [green]kept canonical; deleted[/green] {cpath.name}")
                resolved += 1
            except OSError as e:
                console.print(f"  [red]delete failed:[/red] {e}")
                failed += 1
        elif choice.startswith("f"):
            try:
                cpath.rename(canonical)
                console.print(f"  [green]promoted conflict to canonical[/green] {canonical.name}")
                resolved += 1
            except OSError as e:
                console.print(f"  [red]rename failed:[/red] {e}")
                failed += 1
        elif choice.startswith("a"):
            raise typer.Abort()
        else:
            console.print("  [dim]kept both; no change[/dim]")

    if failed:
        console.print(f"\n[bold]Resolved {resolved} of {len(hits)}; {failed} failed.[/bold]")
    else:
        console.print(f"\n[bold]Resolved {resolved} of {len(hits)}.[/bold]")
    return resolved, failed


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
                console.print(f"  [dim]{prefix} (age {age_days}d):[/dim] {cpath}")
            if not dry_run:
                try:
                    cpath.unlink()
                    reaped += 1
                except OSError:
                    pass
    label = "would reap" if dry_run else "reaped"
    console.print(
        f"[bold]{label}[/bold] {reaped} stale conflict files "
        f"(older than {CONFLICT_AGE_DAYS} days)"
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
    if not CONFIG_PATH.exists():
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
            f"mm: {verb} failed - unexpected config error "
            f"(see auto{verb}.log)",
            file=sys.stderr,
        )
        _log_unexpected(verb, e)
        _write_autorun_breadcrumb(verb, "config-error", type(e).__name__)
        return None

    try:
        passphrase = get_passphrase(non_interactive=True)
    except CryptoError as e:
        print(f"mm: {verb} skipped - {e}", file=sys.stderr)
        _write_autorun_breadcrumb(verb, "no-passphrase")
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
            f"mm: {verb} failed - unexpected crypto error "
            f"(see auto{verb}.log)",
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
            setup.config, setup.passphrase, setup.memory_kb,
            quiet=True, conflict_mode="keep-both",
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
                    f"mm: {result.total_conflicted} conflicts - "
                    "run 'mm conflicts' to review",
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
            degradations.append(
                f"fsync failed on {result.durability_fsync_failures} parent dir(s)"
            )
        if result.corrupt_peer_count:
            degradations.append(
                f"{result.corrupt_peer_count} corrupt peer manifest(s)"
            )
        if result.total_skipped_unknown_source:
            degradations.append(
                f"{result.total_skipped_unknown_source} unknown source(s)"
            )
        if result.total_failed:
            degradations.append(f"{result.total_failed} file(s) failed")

        if degradations:
            _write_autorun_breadcrumb("pull", "degraded", "; ".join(degradations))
        else:
            _write_autorun_breadcrumb("pull", "success")
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

    try:
        acquire_lock()
    except LockError:
        _write_autorun_breadcrumb("push", "lock-held")
        return

    try:
        result = _push_core(
            setup.config, setup.passphrase, setup.memory_kb, quiet=True,
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
