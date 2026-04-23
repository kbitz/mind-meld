"""Mind Meld CLI — built with Typer.

Commands: init, push, pull, status, devices, diff, gc, autopull, autopush,
          sources, conflicts, resolve.
"""

from __future__ import annotations

import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable
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
from mind_meld.devices import list_devices, register_device, update_last_seen
from mind_meld.errors import CryptoError, LockError, ManifestError, MindMeldError, StorageError
from mind_meld.lockfile import acquire_lock, release_lock
from mind_meld.manifest import (
    CONFLICT_INFIX,
    DiffResult,
    TOMBSTONE_TTL_DAYS,
    build_manifest_v2,
    collect_tombstones,
    deserialize_manifest,
    diff_manifests,
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
    """
    total_written: int = 0
    total_merged: int = 0
    total_skipped: int = 0
    total_conflicted: int = 0
    total_failed: int = 0
    bytes_transferred: int = 0
    device_names: list[str] = field(default_factory=list)
    elapsed: float = 0.0

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
    console.print(f"[red]Error:[/red] {msg}")
    raise typer.Exit(1)


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
    manifest_key = f"manifests/{device_id}/manifest.json.enc"
    manifests: list[dict] = []

    is_valid_manifest = _make_manifest_validator(passphrase, memory_kb)
    canonical_exists = backend.exists(manifest_key)
    conflict_copies = backend.find_conflict_copies(manifest_key, is_valid_manifest)
    had_any_source = canonical_exists or bool(conflict_copies)

    # Try canonical manifest. Storage-layer errors (OSError, StorageError, any
    # MindMeldError) are treated as "this copy unreadable" — try conflict
    # copies next. Without this, a TOCTOU race between backend.exists() and
    # backend.get(), or a permission flip mid-scan, would escape as an
    # exception and crash recovery.
    if canonical_exists:
        try:
            enc_data = backend.get(manifest_key)
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
        if not quiet:
            console.print(
                "[yellow]Warning:[/yellow] remote manifest corrupt; "
                "recovered prior state from local sidecar "
                f"({sidecar.sidecar_path()})."
            )
        return sidecar_manifest

    # No sidecar — try peer fallback
    peer_tombstones = _collect_peer_tombstones(
        backend, device_id, passphrase, memory_kb
    )
    if peer_tombstones:
        if not quiet:
            console.print(
                f"[yellow]Warning:[/yellow] remote manifest corrupt and no "
                f"local sidecar; recovered {len(peer_tombstones)} tombstone(s) "
                f"from peer device(s). Recent local deletions may be lost — "
                f"verify no files have resurrected."
            )
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
        devices = list_devices(backend)
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
    """
    # Sort by timestamp so later manifests overwrite earlier ones
    sorted_manifests = sorted(
        manifests,
        key=lambda m: m.get("timestamp", ""),
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
    # v1 compat: update top-level "files" from claude source
    if "claude" in merged_sources:
        merged["files"] = merged_sources["claude"].get("files", {})

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
    manifest_key = f"manifests/{device_id}/manifest.json.enc"
    is_valid = _make_manifest_validator(passphrase, memory_kb)
    return backend.delete_conflict_copies(manifest_key, is_valid)


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
        blob_key = f"data/{device_id}/{info['sha256']}.enc"
        backend.put(blob_key, enc_data)
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
    """
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    device_short = (device_id or "unknown")[:8]

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

def _apply_incoming_file(
    local_path: Path,
    rel_path: str,
    plain_data: bytes,
    remote_info: dict,
    remote_device_id: str,
    interactive_resolve: bool = False,
    verbose: bool = False,
) -> ApplyOutcome:
    """Apply one decrypted remote file to the local tree.

    See the decision-tree comment above for branch semantics. The local file
    is never destroyed without a recoverable trail (either conflict copy
    or rollback).
    """
    local_path.parent.mkdir(parents=True, exist_ok=True)

    # [W] local has no copy yet.
    if not local_path.exists():
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

    # Re-read local state. Precomputed snapshot can be stale if the user
    # edited the file after _pull_core built its diff.
    try:
        local_hash = hash_file(local_path)
    except (PermissionError, OSError) as e:
        console.print(f"  [yellow]read failed:[/yellow] {rel_path} \u2014 {e}")
        return "failed"

    remote_hash = remote_info.get("sha256")

    # [U] already in sync.
    if local_hash == remote_hash:
        return "unchanged"

    # [M] mergeable: jsonl / MEMORY.md are line-union safe.
    if should_merge(rel_path):
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
        # choice == "keep-both" -> fall through to default conflict path

    conflict_path = conflict_filename(local_path, remote_device_id)
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
        blob_key = f"data/{source_device_id}/{info['sha256']}.enc"
        try:
            enc_data = backend.get(blob_key)
        except MindMeldError:
            if verbose:
                console.print(f"  [yellow]blob missing: {blob_key}[/yellow]")
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


def _delete_files(
    base_path: Path,
    to_delete: list[str],
    verbose: bool = False,
) -> int:
    """Delete files from base_path. Returns count of files deleted."""
    count = 0
    for rel_path in to_delete:
        local_path = base_path / rel_path
        if local_path.exists():
            local_path.unlink()
            count += 1
            if verbose:
                console.print(f"  [red]\u2715[/red] {rel_path}")
    return count


# ── init ──────────────────────────────────────────────────────────────


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
    """
    if CONFIG_PATH.exists():
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
        if not quiet:
            console.print("[yellow]No sync sources found. Run 'mm init' to configure.[/yellow]")
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

    for src_name, src_data in local_manifest["sources"].items():
        remote_src = remote_sources.get(src_name, {"files": {}})
        diff = diff_manifests(
            {"files": src_data["files"]},
            {"files": remote_src.get("files", {})},
        )

        if not diff.has_changes:
            continue

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
    manifest_key = f"manifests/{device_id}/manifest.json.enc"
    backend.put(manifest_key, enc_manifest)

    # Write sidecar (best-effort: failure warns but does not abort push;
    # the remote manifest succeeded, so peers still have a path to recovery).
    try:
        sidecar.write(local_manifest)
    except (OSError, StorageError) as e:
        if not quiet:
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
    no_prompt: bool = typer.Option(
        False, "--no-prompt",
        help="Don't prompt on conflicts — default keep-both (for scripting)",
    ),
    resolve_interactive: bool = typer.Option(
        False, "--resolve-interactive",
        help="Prompt for each conflict (default: auto keep-both)",
    ),
) -> None:
    """Pull session data from storage to local.

    Conflicts (local edited, remote differs) default to keep-both: remote
    wins the canonical path, local is renamed to .sync-conflict-*. Use
    --resolve-interactive to pick per-file at pull time.
    """
    if no_prompt and resolve_interactive:
        _error("--no-prompt and --resolve-interactive are mutually exclusive")

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
            interactive_resolve=resolve_interactive,
        )
    finally:
        release_lock()


def _pull_core(
    config: dict,
    passphrase: str,
    memory_kb: int,
    from_device: str | None = None,
    source_filter: str | None = None,
    verbose: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
    interactive_resolve: bool = False,
) -> PullResult:
    """Core pull logic shared by pull and autopull.

    Additive-only: downloads new and modified files, never deletes local files.
    Tombstoned files are skipped. JSONL files are merged. MEMORY.md files are
    line-merged. Non-mergeable files with divergent local edits are handled
    per the _apply_incoming_file decision tree: skip (local newer),
    conflict-copy (remote wins canonical, local preserved as .sync-conflict-*),
    or interactively resolved if interactive_resolve=True.

    When quiet=True, suppresses all rich console output (for autopull).
    """
    start = time.time()
    my_device_id = config["device"]["id"]

    backend = get_backend(config)

    # Build local source path map (use LOCAL config, not remote manifest base_path)
    local_sources_map: dict[str, Path] = {}
    for src_cfg in get_sources(config):
        local_sources_map[src_cfg["name"]] = Path(src_cfg["path"]).expanduser().resolve()

    # Find devices to pull from
    devices = list_devices(backend)
    if from_device:
        devices = [d for d in devices if d["device_id"] == from_device]
        if not devices and not quiet:
            _error(f"Device not found: {from_device}")
    else:
        devices = [d for d in devices if d["device_id"] != my_device_id]

    if not devices:
        if not quiet:
            console.print("[yellow]No other devices found to pull from.[/yellow]")
        return PullResult(elapsed=time.time() - start)

    # Pre-fetch all device manifests (used for tombstone collection + pull).
    # Missing and corrupt manifests are both mapped to None — pull is read-
    # only on remote state, so skipping either is safe. Corrupt manifests
    # are surfaced as a warning so the user can investigate.
    all_devices_list = list_devices(backend)
    manifest_cache: dict[str, dict | None] = {}
    for d in all_devices_list:
        did = d["device_id"]
        peer_fetch = _fetch_remote_manifest(backend, did, passphrase, memory_kb)
        if peer_fetch.status == "corrupt" and not quiet:
            console.print(
                f"[yellow]Warning:[/yellow] manifest for device "
                f"{d.get('device_name', did)} ({did}) is corrupt — skipping "
                f"pull from this device."
            )
        manifest_cache[did] = peer_fetch.manifest if peer_fetch.is_ok else None

    # Pre-collect all tombstones from ALL device manifests for O(1) lookup
    all_tombstones = collect_tombstones(
        list(manifest_cache.keys()),
        lambda did: manifest_cache.get(did),
    )

    total_written = 0
    total_merged = 0
    total_skipped = 0
    total_conflicted = 0
    total_failed = 0
    bytes_transferred = 0
    device_names: list[str] = []
    # Deferred durability: per-file writes skip fsync; at the end of pull
    # we fsync each unique parent directory once so recent renames are
    # durable against crash / power loss.
    touched_parents: set[Path] = set()

    for device in devices:
        did = device["device_id"]
        dname = device["device_name"]
        if not quiet:
            console.print(f"\n[bold]Pulling from {dname} ({did})...[/bold]")

        remote_manifest = manifest_cache.get(did)
        if remote_manifest is None:
            if not quiet:
                console.print(f"  [yellow]No manifest for {dname}[/yellow]")
            continue

        # manifest_cache values come from _fetch_remote_manifest → load_manifest
        # → normalized v2 shape guaranteed.
        remote_sources = remote_manifest.get("sources", {})

        device_had_changes = False

        for src_name, src_data in remote_sources.items():
            if source_filter and src_name != source_filter:
                continue

            if src_name not in local_sources_map:
                if verbose and not quiet:
                    console.print(f"  [dim]skipping unknown source '{src_name}'[/dim]")
                continue

            remote_files = src_data.get("files", {})
            if not remote_files:
                continue

            base_path = local_sources_map[src_name]

            if verbose and not quiet:
                console.print(f"  [bold]Source '{src_name}' ({base_path}):[/bold]")

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

            diff = diff_manifests(
                {"files": remote_files},
                {"files": local_files},
            )

            if dry_run:
                if not quiet:
                    console.print(f"  Dry run for {dname}/{src_name}:")
                    _print_pull_prediction(diff, base_path, src_name)
                continue

            # Check if there's anything to download (ignore deleted — additive model)
            to_download = {**diff.new, **diff.modified}

            # Filter out tombstoned files
            to_download = {
                path: info for path, info in to_download.items()
                if not is_tombstoned(src_name, path, all_tombstones)
            }

            if not to_download:
                if verbose and not quiet:
                    console.print(f"  [green]Up to date with {dname}/{src_name}.[/green]")
                continue

            bt, outcomes = _download_and_apply(
                backend, base_path, to_download, did, passphrase, memory_kb,
                interactive_resolve=interactive_resolve,
                verbose=(verbose and not quiet),
            )
            bytes_transferred += bt

            # Collect parent dirs of every file that was successfully
            # written (any outcome except "skipped" / "unchanged" / "failed").
            # These get a single F_FULLFSYNC at end-of-pull.
            for rel in (
                outcomes["written"] + outcomes["merged"] + outcomes["conflicted"]
            ):
                touched_parents.add((base_path / rel).parent)

            src_written = len(outcomes["written"])
            src_merged = len(outcomes["merged"])
            src_skipped = len(outcomes["skipped"])
            src_conflicted = len(outcomes["conflicted"])
            src_failed = len(outcomes["failed"])

            total_written += src_written
            total_merged += src_merged
            total_skipped += src_skipped
            total_conflicted += src_conflicted
            total_failed += src_failed

            # "had changes" drives the manifest-level iCloud conflict-copy
            # cleanup downstream. Fire if we *processed* any files for this
            # device, including skipped/failed — otherwise one-way-sync
            # setups (always local-newer) never run the manifest cleanup
            # and accumulate iCloud duplicates forever.
            if src_written + src_merged + src_conflicted + src_skipped + src_failed > 0:
                device_had_changes = True

            # Per-source status line. Conflicts/failures are load-bearing —
            # print them even without --verbose so the user notices.
            if not quiet and (src_conflicted or src_failed or verbose):
                line = f"  [bold]{src_name}:[/bold]"
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

            # Write sync log only for claude source. Group by outcome so a user
            # reading .mind-meld-log.md can tell what was applied vs what needs
            # manual conflict resolution.
            if src_name == "claude":
                claude_dir = str(base_path)
                logs = write_sync_log(
                    claude_dir=claude_dir,
                    device_name=dname,
                    device_id=did,
                    new_files=outcomes["written"],
                    modified_files=outcomes["merged"],
                    deleted_files=[],
                    conflicted_files=outcomes["conflicted"],
                    skipped_files=outcomes["skipped"],
                )
                if verbose and not quiet and logs:
                    for log in logs:
                        console.print(f"  [dim]wrote sync log: {log}[/dim]")

        if device_had_changes:
            device_names.append(dname)
            # Clean up iCloud/Dropbox manifest conflict copies. Unrelated to
            # .sync-conflict-* file copies — those live in the synced tree.
            _cleanup_conflict_copies(backend, did, passphrase, memory_kb)

    # Deferred-durability commit: fsync each unique parent directory we
    # wrote into so recent renames survive crash or power loss. A failure
    # here means some of this pull's renames may be non-durable — warn
    # but don't roll back (files are already in place; a subsequent pull
    # will simply re-apply if needed).
    for parent_dir in sorted(touched_parents):
        try:
            fsutil.fsync_dir(parent_dir)
        except StorageError as e:
            if not quiet:
                console.print(
                    f"  [yellow]warning:[/yellow] durability fsync failed "
                    f"on {parent_dir} — {e}"
                )

    elapsed = time.time() - start
    result = PullResult(
        total_written=total_written,
        total_merged=total_merged,
        total_skipped=total_skipped,
        total_conflicted=total_conflicted,
        total_failed=total_failed,
        bytes_transferred=bytes_transferred,
        device_names=device_names,
        elapsed=elapsed,
    )

    if not quiet:
        console.print(f"\n[bold green]Pull complete.[/bold green]")
        parts = []
        if total_written:
            parts.append(f"{total_written} written")
        if total_merged:
            parts.append(f"{total_merged} merged")
        if total_skipped:
            parts.append(f"{total_skipped} skipped (local newer)")
        if total_conflicted:
            parts.append(f"[yellow]{total_conflicted} conflicts[/yellow]")
        if total_failed:
            parts.append(f"[red]{total_failed} failed[/red]")
        if parts:
            console.print("  " + ", ".join(parts))
        else:
            console.print("  nothing to apply")
        if bytes_transferred:
            mb = bytes_transferred / (1024 * 1024)
            console.print(f"  {mb:.1f}MB transferred")
        console.print(f"  Completed in {elapsed:.1f}s")
        if total_conflicted:
            console.print(
                "  [yellow]Run [bold]mm conflicts[/bold] to review, "
                "[bold]mm resolve[/bold] to pick a winner.[/yellow]"
            )

    return result


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
    devices = list_devices(backend)

    console.print(f"\n[bold]Mind Meld Status[/bold]")
    console.print(f"  Device: {device_name} ({device_id})")
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

    for src_name, src_data in local_manifest["sources"].items():
        if source and src_name != source:
            continue

        local_files = src_data["files"]
        remote_src = remote_sources.get(src_name, {"files": {}})
        remote_files = remote_src.get("files", {})

        diff = diff_manifests(
            {"files": local_files},
            {"files": remote_files},
        )

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


# ── devices ───────────────────────────────────────────────────────────


@app.command()
def devices() -> None:
    """List all registered devices."""
    config = _get_config()
    backend = get_backend(config)
    device_list = list_devices(backend)
    my_id = config["device"]["id"]

    if not device_list:
        console.print("[yellow]No devices registered.[/yellow]")
        return

    table = Table(title="Registered Devices")
    table.add_column("Name")
    table.add_column("ID")
    table.add_column("Last Seen")
    table.add_column("")

    for d in device_list:
        marker = "[green]\u2190 this device[/green]" if d["device_id"] == my_id else ""
        table.add_row(
            d.get("device_name", "?"),
            d["device_id"],
            d.get("last_seen", "?"),
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
    for src_name, src_data in local_manifest["sources"].items():
        if source and src_name != source:
            continue

        remote_src = remote_sources.get(src_name, {"files": {}})
        remote_files = remote_src.get("files", {})
        diff = diff_manifests(
            {"files": src_data["files"]},
            {"files": remote_files},
        )

        if not diff.has_changes:
            continue

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
    devices = list_devices(backend)
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
    all_blobs = backend.list_keys("data/")
    orphan_count = 0

    for blob_key in all_blobs:
        if not blob_key.endswith(".enc"):
            continue
        # Extract hash from key: data/{device_id}/{sha256}.enc
        parts = blob_key.split("/")
        if len(parts) != 3:
            continue
        sha = parts[2].removesuffix(".enc")
        if sha not in referenced_hashes:
            orphan_count += 1
            if verbose:
                console.print(f"  [red]orphan:[/red] {blob_key}")
            if not dry_run:
                backend.delete(blob_key)

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
        _resolve_interactive_loop(hits)
    finally:
        release_lock()


def _resolve_interactive_loop(hits: list[tuple[str, Path, Path | None]]) -> None:
    """Walk each conflict and prompt for resolution. Extracted so `resolve`
    stays a thin wrapper around acquire/release lock boilerplate."""
    import difflib

    resolved = 0
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
            elif choice.startswith("d"):
                try:
                    cpath.unlink()
                    console.print(f"  [red]deleted[/red] {cpath.name}")
                    resolved += 1
                except OSError as e:
                    console.print(f"  [red]delete failed:[/red] {e}")
            continue

        try:
            local_text = canonical.read_text(errors="replace").splitlines()
            conflict_text = cpath.read_text(errors="replace").splitlines()
        except OSError as e:
            console.print(f"  [red]read failed:[/red] {e}")
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
        elif choice.startswith("f"):
            try:
                cpath.rename(canonical)
                console.print(f"  [green]promoted conflict to canonical[/green] {canonical.name}")
                resolved += 1
            except OSError as e:
                console.print(f"  [red]rename failed:[/red] {e}")
        elif choice.startswith("a"):
            raise typer.Abort()
        else:
            console.print("  [dim]kept both; no change[/dim]")

    console.print(f"\n[bold]Resolved {resolved} of {len(hits)}.[/bold]")


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


# ── autopull ──────────────────────────────────────────────────────────


@app.command()
def autopull() -> None:
    """Pull changes silently. Designed for Claude Code — no prompts, minimal output."""
    try:
        config = load_config()
    except Exception:
        return  # not initialized — silent exit

    try:
        passphrase = get_passphrase()
    except Exception:
        print("mm: no passphrase available \u2014 skipping pull", file=sys.stderr)
        return

    try:
        acquire_lock()
    except LockError:
        return  # another operation running — don't block Claude

    try:
        backend = get_backend(config)
        try:
            memory_kb = _init_crypto_session(backend, passphrase, config)
        except MindMeldError as e:
            print(f"mm: pull failed — {e}", file=sys.stderr)
            return
        # autopull is always silent + never-prompt (Claude Code hook context)
        result = _pull_core(config, passphrase, memory_kb, quiet=True, interactive_resolve=False)

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
                    f"mm: {result.total_conflicted} conflicts — "
                    "run 'mm conflicts' to review",
                    file=sys.stderr,
                )

    except Exception as e:
        print(f"mm: pull failed \u2014 {e}", file=sys.stderr)
    finally:
        release_lock()


# ── autopush ─────────────────────────────────────────────────────────


@app.command()
def autopush() -> None:
    """Push changes silently. Designed for Claude Code — no prompts, minimal output."""
    try:
        config = load_config()
    except Exception:
        return  # not initialized — silent exit

    try:
        passphrase = get_passphrase()
    except Exception:
        print("mm: no passphrase available \u2014 skipping push", file=sys.stderr)
        return

    try:
        acquire_lock()
    except LockError:
        return  # another operation running — don't block Claude

    try:
        backend = get_backend(config)
        try:
            memory_kb = _init_crypto_session(backend, passphrase, config)
        except MindMeldError as e:
            print(f"mm: push failed — {e}", file=sys.stderr)
            return
        # No auto-GC on autopush (prevents blob-deletion hole)
        result = _push_core(config, passphrase, memory_kb, quiet=True)

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

    except Exception as e:
        print(f"mm: push failed \u2014 {e}", file=sys.stderr)
    finally:
        release_lock()


# ── helpers ───────────────────────────────────────────────────────────


def _default_device_name() -> str:
    """Generate a default device name from hostname."""
    import socket

    return socket.gethostname()


if __name__ == "__main__":
    app()
