"""MemSync CLI — built with Typer.

Commands: init, push, pull, status, devices, diff, gc, autopull, autopush, sources
"""

from __future__ import annotations

import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from memsync import __version__
from memsync.config import CONFIG_PATH, DEFAULT_STORAGE_PATH, load_config, save_config, get_sources
from memsync.crypto import (
    decrypt,
    encrypt,
    get_passphrase,
    store_passphrase_in_keyring,
)
from memsync.devices import list_devices, register_device, update_last_seen
from memsync.errors import CryptoError, LockError, ManifestError, MemSyncError
from memsync.lockfile import acquire_lock, release_lock
from memsync.manifest import (
    DiffResult,
    TOMBSTONE_TTL_DAYS,
    build_manifest_v2,
    collect_tombstones,
    normalize_manifest,
    read_and_hash,
    deserialize_manifest,
    diff_manifests,
    generate_tombstones,
    serialize_manifest,
    hash_file,
    is_tombstoned,
)
from memsync.merge import merge_file, should_merge
from memsync.storage import get_backend
from memsync.synclog import write_sync_log


@dataclass
class PullResult:
    """Result of a pull operation."""
    total_new: int = 0
    total_modified: int = 0
    bytes_transferred: int = 0
    device_names: list[str] = field(default_factory=list)
    elapsed: float = 0.0


@dataclass
class PushResult:
    """Result of a push operation."""
    total_new: int = 0
    total_modified: int = 0
    total_deleted: int = 0
    bytes_transferred: int = 0
    elapsed: float = 0.0

app = typer.Typer(
    name="msync",
    help="MemSync — sync Claude Code sessions and other sources across machines.",
    add_completion=False,
)
console = Console()


def _error(msg: str) -> None:
    console.print(f"[red]Error:[/red] {msg}")
    raise typer.Exit(1)


def _get_config() -> dict:
    try:
        return load_config()
    except MemSyncError as e:
        _error(str(e))
        raise  # unreachable, but keeps type checker happy


def _get_passphrase_or_exit() -> str:
    try:
        return get_passphrase()
    except CryptoError as e:
        _error(str(e))
        raise


def _fetch_remote_manifest(
    backend, device_id: str, passphrase: str, memory_kb: int
) -> dict | None:
    """Fetch and decrypt remote manifest, merging any conflict copies.

    Read-only: does NOT delete conflict copies. Use _cleanup_conflict_copies()
    after the manifest has been successfully used in a mutating operation.

    If the canonical manifest and/or conflict copies exist, decrypt all that
    succeed and merge additively (union of files, latest manifest timestamp wins
    for duplicate paths). Returns None only if ALL copies fail.
    """
    manifest_key = f"manifests/{device_id}/manifest.json.enc"
    manifests: list[dict] = []

    # Try canonical manifest
    if backend.exists(manifest_key):
        try:
            enc_data = backend.get(manifest_key)
            plain = decrypt(enc_data, passphrase, memory_kb)
            manifests.append(deserialize_manifest(plain))
        except (CryptoError, ManifestError):
            pass  # canonical corrupt — try conflict copies

    # Try conflict copies (iCloud/Dropbox)
    for conflict_path in backend.find_conflict_copies(manifest_key):
        try:
            enc_data = conflict_path.read_bytes()
            plain = decrypt(enc_data, passphrase, memory_kb)
            manifests.append(deserialize_manifest(plain))
        except (CryptoError, ManifestError, OSError):
            pass  # skip unreadable conflict copies

    if not manifests:
        if backend.exists(manifest_key):
            console.print(
                "[yellow]Warning:[/yellow] manifest corrupt or unreadable. "
                "Will perform full re-push."
            )
        return None

    if len(manifests) == 1:
        return manifests[0]

    # Merge multiple manifests additively: union of files, latest timestamp wins
    return _merge_manifests(manifests)


def _merge_manifests(manifests: list[dict]) -> dict:
    """Merge multiple manifest variants additively.

    For each source, takes the union of all files. When the same relative path
    appears in multiple manifests, the entry from the manifest with the latest
    timestamp wins.
    """
    # Sort by timestamp so later manifests overwrite earlier ones
    sorted_manifests = sorted(
        manifests,
        key=lambda m: m.get("timestamp", ""),
    )

    merged = dict(sorted_manifests[-1])  # start with latest as base
    merged_sources: dict[str, dict] = {}

    for m in sorted_manifests:
        normalize_manifest(m)
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


def _cleanup_conflict_copies(backend, device_id: str) -> int:
    """Delete conflict copies for a device's manifest.

    Call ONLY from mutating operations (push, pull) after the manifest
    has been successfully used. Never from status, diff, gc, or dry-run.
    """
    manifest_key = f"manifests/{device_id}/manifest.json.enc"
    return backend.delete_conflict_copies(manifest_key)


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


def _download_and_apply(
    backend,
    base_path: Path,
    to_download: dict[str, dict],
    source_device_id: str,
    passphrase: str,
    memory_kb: int,
    verbose: bool = False,
) -> int:
    """Download blobs and apply them locally.

    For .jsonl files: merges with existing local content instead of overwriting.
    For other files: atomic write via tmp + rename.

    Returns total encrypted bytes transferred.
    """
    bytes_transferred = 0
    for rel_path, info in to_download.items():
        blob_key = f"data/{source_device_id}/{info['sha256']}.enc"
        try:
            enc_data = backend.get(blob_key)
        except MemSyncError:
            if verbose:
                console.print(f"  [yellow]blob missing: {blob_key}[/yellow]")
            continue

        plain_data = decrypt(enc_data, passphrase, memory_kb)
        bytes_transferred += len(enc_data)

        local_path = base_path / rel_path
        local_path.parent.mkdir(parents=True, exist_ok=True)

        if should_merge(rel_path) and local_path.exists():
            local_bytes = local_path.read_bytes()
            merged = merge_file(rel_path, local_bytes, plain_data)
            tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
            tmp_path.write_bytes(merged)
            tmp_path.rename(local_path)
        else:
            tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
            tmp_path.write_bytes(plain_data)
            tmp_path.rename(local_path)

        if verbose:
            console.print(f"  [green]\u2193[/green] {rel_path}")

    return bytes_transferred


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
    """Initialize MemSync: generate device ID, configure iCloud storage, set passphrase."""
    if CONFIG_PATH.exists():
        overwrite = typer.confirm(
            f"Config already exists at {CONFIG_PATH}. Overwrite?"
        )
        if not overwrite:
            raise typer.Exit()

    console.print(f"[bold]MemSync v{__version__} \u2014 init[/bold]\n")

    # Device
    device_id = uuid.uuid4().hex[:8]
    device_name = typer.prompt("Device name", default=_default_device_name())

    # Storage path (iCloud Drive by default)
    storage_path = typer.prompt("Storage folder path", default=DEFAULT_STORAGE_PATH)

    config: dict = {
        "device": {"id": device_id, "name": device_name},
        "storage": {"path": storage_path},
        "sync": {"claude_dir": "~/.claude", "max_file_size": 52_428_800},
        "crypto": {"argon2_memory_kb": 65_536},
    }

    # Create directory
    full_path = Path(storage_path).expanduser()
    full_path.mkdir(parents=True, exist_ok=True)
    console.print(f"  Storage: {full_path}")

    # Passphrase
    passphrase = typer.prompt("Encryption passphrase", hide_input=True)
    if not passphrase:
        _error("Passphrase cannot be empty.")
    passphrase_confirm = typer.prompt("Confirm passphrase", hide_input=True)
    if passphrase != passphrase_confirm:
        _error("Passphrases don't match.")

    # Store in keyring
    if store_passphrase_in_keyring(passphrase):
        console.print("  Passphrase stored in OS keyring.")
    else:
        console.print(
            "  [yellow]No keyring available.[/yellow] "
            "Set MEMSYNC_PASSPHRASE environment variable instead."
        )

    # Save config
    save_config(config)
    console.print(f"  Config written to {CONFIG_PATH}")

    # Check for gstack and offer to add as sync source
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
            console.print("  gstack source added to config.")

    # Register device
    backend = get_backend(load_config())
    register_device(backend, device_id, device_name)
    console.print(f"  Device registered: {device_name} ({device_id})")

    console.print("\n[green]MemSync initialized. Run 'msync push' to sync.[/green]")


# ── push ──────────────────────────────────────────────────────────────


@app.command()
def push(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed output"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change"),
) -> None:
    """Push local session data to storage."""
    config = _get_config()
    passphrase = _get_passphrase_or_exit()
    memory_kb = config["crypto"]["argon2_memory_kb"]

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    try:
        result = _push_core(config, passphrase, memory_kb, verbose, dry_run)

        # Auto GC on interactive push only (not autopush)
        if result and (result.total_new or result.total_modified or result.total_deleted):
            try:
                gc_count = _do_gc(config, passphrase, memory_kb, dry_run=False, verbose=False)
                if gc_count:
                    console.print(f"  GC: deleted {gc_count} orphaned blobs.")
            except Exception:
                pass  # GC failure must not break push
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
            console.print("[yellow]No sync sources found. Run 'msync init' to configure.[/yellow]")
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

    # Fetch remote manifest
    remote_manifest = _fetch_remote_manifest(backend, device_id, passphrase, memory_kb)
    if remote_manifest:
        normalize_manifest(remote_manifest)

    # Generate tombstones for files that disappeared since last push
    tombstones = generate_tombstones(local_manifest, remote_manifest, device_id)
    local_manifest["tombstones"] = tombstones

    # Diff and upload per-source
    total_bytes = 0
    total_new = 0
    total_modified = 0
    total_deleted = 0

    remote_sources = remote_manifest.get("sources", {}) if remote_manifest else {}

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

    if not (total_new or total_modified or total_deleted):
        if not quiet:
            console.print("[green]Nothing to push \u2014 everything is up to date.[/green]")
        return None

    # Upload manifest (includes tombstones)
    if not quiet:
        console.print(f"\n[bold]Uploading {total_new + total_modified} files...[/bold]")
    manifest_data = serialize_manifest(local_manifest)
    enc_manifest = encrypt(manifest_data, passphrase, memory_kb)
    manifest_key = f"manifests/{device_id}/manifest.json.enc"
    backend.put(manifest_key, enc_manifest)

    # Update device last_seen
    update_last_seen(backend, device_id)

    # Clean up conflict copies (write-path only)
    _cleanup_conflict_copies(backend, device_id)

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
) -> None:
    """Pull session data from storage to local."""
    config = _get_config()
    passphrase = _get_passphrase_or_exit()
    memory_kb = config["crypto"]["argon2_memory_kb"]

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    try:
        _pull_core(config, passphrase, memory_kb, from_device, source, verbose, dry_run)
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
) -> PullResult:
    """Core pull logic shared by pull and autopull.

    Additive-only: downloads new and modified files, never deletes local files.
    Tombstoned files are skipped. JSONL files are merged. MEMORY.md files are
    line-merged.

    When quiet=True, suppresses all rich console output (for autopush).
    Returns PullResult with counts and device names.
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

    # Pre-fetch all device manifests (used for tombstone collection + pull)
    all_devices_list = list_devices(backend)
    manifest_cache: dict[str, dict | None] = {}
    for d in all_devices_list:
        manifest_cache[d["device_id"]] = _fetch_remote_manifest(
            backend, d["device_id"], passphrase, memory_kb
        )

    # Pre-collect all tombstones from ALL device manifests for O(1) lookup
    all_tombstones = collect_tombstones(
        list(manifest_cache.keys()),
        lambda did: manifest_cache.get(did),
    )

    total_new = 0
    total_modified = 0
    bytes_transferred = 0
    device_names: list[str] = []

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

        normalize_manifest(remote_manifest)
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

            # Build local state for comparison
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
                    _print_diff_summary(diff, 0)
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

            device_had_changes = True
            bytes_transferred += _download_and_apply(
                backend, base_path, to_download, did, passphrase, memory_kb,
                verbose=(verbose and not quiet),
            )

            total_new += len(to_download)

            # Write sync log only for claude source (no deleted_files in additive model)
            if src_name == "claude":
                claude_dir = str(base_path)
                logs = write_sync_log(
                    claude_dir=claude_dir,
                    device_name=dname,
                    device_id=did,
                    new_files=list(to_download.keys()),
                    modified_files=[],
                    deleted_files=[],
                )
                if verbose and not quiet and logs:
                    for log in logs:
                        console.print(f"  [dim]wrote sync log: {log}[/dim]")

        if device_had_changes:
            device_names.append(dname)
            # Clean up conflict copies (write-path only)
            _cleanup_conflict_copies(backend, did)

    elapsed = time.time() - start
    result = PullResult(
        total_new=total_new,
        total_modified=total_modified,
        bytes_transferred=bytes_transferred,
        device_names=device_names,
        elapsed=elapsed,
    )

    if not quiet:
        console.print(f"\n[bold green]Pull complete.[/bold green]")
        console.print(f"  {total_new} files synced")
        if bytes_transferred:
            mb = bytes_transferred / (1024 * 1024)
            console.print(f"  {mb:.1f}MB transferred")
        console.print(f"  Completed in {elapsed:.1f}s")

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
    memory_kb = config["crypto"]["argon2_memory_kb"]
    device_id = config["device"]["id"]
    device_name = config["device"]["name"]
    max_file_size = config["sync"]["max_file_size"]

    backend = get_backend(config)

    # Build local manifest (v2)
    sources_configs = get_sources(config)
    local_manifest = build_manifest_v2(
        device_id, device_name, sources_configs, max_file_size
    )

    # Fetch remote manifest
    remote_manifest = _fetch_remote_manifest(backend, device_id, passphrase, memory_kb)
    if remote_manifest:
        normalize_manifest(remote_manifest)

    remote_sources = remote_manifest.get("sources", {}) if remote_manifest else {}

    # Devices
    devices = list_devices(backend)

    console.print(f"\n[bold]MemSync Status[/bold]")
    console.print(f"  Device: {device_name} ({device_id})")

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
    memory_kb = config["crypto"]["argon2_memory_kb"]
    device_id = config["device"]["id"]
    device_name = config["device"]["name"]
    max_file_size = config["sync"]["max_file_size"]

    backend = get_backend(config)

    target_id = from_device or device_id

    # Build local manifest (v2)
    sources_configs = get_sources(config)
    local_manifest = build_manifest_v2(
        device_id, device_name, sources_configs, max_file_size
    )

    remote_manifest = _fetch_remote_manifest(backend, target_id, passphrase, memory_kb)
    if remote_manifest:
        normalize_manifest(remote_manifest)

    remote_sources = remote_manifest.get("sources", {}) if remote_manifest else {}

    console.print(f"\n[bold]Diff against {'device ' + target_id if from_device else 'remote'}:[/bold]")

    any_changes = False
    for src_name, src_data in local_manifest["sources"].items():
        if source and src_name != source:
            continue

        remote_src = remote_sources.get(src_name, {"files": {}})
        diff = diff_manifests(
            {"files": src_data["files"]},
            {"files": remote_src.get("files", {})},
        )

        if not diff.has_changes:
            continue

        any_changes = True
        console.print(f"\n  [bold]Source '{src_name}':[/bold]")
        for path in sorted(diff.new):
            console.print(f"    [green]+ {path}[/green]")
        for path in sorted(diff.modified):
            console.print(f"    [yellow]~ {path}[/yellow]")
        for path in sorted(diff.deleted):
            console.print(f"    [red]- {path}[/red]")

    if not any_changes:
        console.print("[green]No differences.[/green]")


# ── gc ────────────────────────────────────────────────────────────────


@app.command()
def gc(
    dry_run: bool = typer.Option(False, "--dry-run", help="List orphans without deleting"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Garbage collect orphaned blobs not referenced by any manifest."""
    config = _get_config()
    passphrase = _get_passphrase_or_exit()
    memory_kb = config["crypto"]["argon2_memory_kb"]

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    try:
        _do_gc(config, passphrase, memory_kb, dry_run, verbose)
    finally:
        release_lock()


def _do_gc(
    config: dict,
    passphrase: str,
    memory_kb: int,
    dry_run: bool,
    verbose: bool,
) -> int:
    """Run garbage collection. Returns number of orphaned blobs found/deleted."""
    backend = get_backend(config)

    # Collect all referenced hashes from ALL device manifests
    devices = list_devices(backend)
    referenced_hashes: set[str] = set()

    for device in devices:
        did = device["device_id"]
        manifest = _fetch_remote_manifest(backend, did, passphrase, memory_kb)
        if manifest is None:
            continue

        normalize_manifest(manifest)

        # Iterate sources.*.files to collect hashes
        for src_data in manifest.get("sources", {}).values():
            for info in src_data.get("files", {}).values():
                referenced_hashes.add(info["sha256"])

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

    from memsync.manifest import walk_source

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
        print("msync: no passphrase available \u2014 skipping pull", file=sys.stderr)
        return

    memory_kb = config["crypto"]["argon2_memory_kb"]

    try:
        acquire_lock()
    except LockError:
        return  # another operation running — don't block Claude

    try:
        result = _pull_core(config, passphrase, memory_kb, quiet=True)

        if result.total_new or result.total_modified:
            parts = []
            if result.total_new:
                parts.append(f"{result.total_new} new")
            if result.total_modified:
                parts.append(f"{result.total_modified} modified")
            src_display = ", ".join(result.device_names)
            total = result.total_new + result.total_modified
            print(f"msync: pulled {total} files from {src_display} ({', '.join(parts)})")

    except Exception as e:
        print(f"msync: pull failed \u2014 {e}", file=sys.stderr)
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
        print("msync: no passphrase available \u2014 skipping push", file=sys.stderr)
        return

    memory_kb = config["crypto"]["argon2_memory_kb"]

    try:
        acquire_lock()
    except LockError:
        return  # another operation running — don't block Claude

    try:
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
            print(f"msync: pushed {total} files ({', '.join(parts)})")

    except Exception as e:
        print(f"msync: push failed \u2014 {e}", file=sys.stderr)
    finally:
        release_lock()


# ── helpers ───────────────────────────────────────────────────────────


def _default_device_name() -> str:
    """Generate a default device name from hostname."""
    import socket

    return socket.gethostname()


if __name__ == "__main__":
    app()
