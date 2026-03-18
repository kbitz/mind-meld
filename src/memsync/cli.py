"""MemSync CLI — built with Typer.

Commands: init, push, pull, status, devices, diff, gc
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from memsync import __version__
from memsync.config import CONFIG_PATH, load_config, save_config
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
    build_manifest,
    deserialize_manifest,
    diff_manifests,
    serialize_manifest,
)
from memsync.paths import rewrite_manifest_paths
from memsync.storage import get_backend
from memsync.synclog import write_sync_log

app = typer.Typer(
    name="msync",
    help="MemSync — sync Claude Code sessions across machines.",
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


def _get_passphrase_or_exit(config: dict) -> str:
    memory_kb = config.get("crypto", {}).get("argon2_memory_kb", 65_536)
    try:
        return get_passphrase(memory_kb)
    except CryptoError as e:
        _error(str(e))
        raise


def _fetch_remote_manifest(
    backend, device_id: str, passphrase: str, memory_kb: int
) -> dict | None:
    """Fetch and decrypt remote manifest. Returns None if missing or corrupt."""
    manifest_key = f"manifests/{device_id}/manifest.json.enc"
    if not backend.exists(manifest_key):
        return None
    try:
        enc_data = backend.get(manifest_key)
        plain = decrypt(enc_data, passphrase, memory_kb)
        return deserialize_manifest(plain)
    except (CryptoError, ManifestError) as e:
        console.print(
            f"[yellow]Warning:[/yellow] manifest corrupt or unreadable ({e}). "
            "Will perform full re-push."
        )
        return None


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


# ── init ──────────────────────────────────────────────────────────────


@app.command()
def init() -> None:
    """Initialize MemSync: generate device ID, configure storage, set passphrase."""
    if CONFIG_PATH.exists():
        overwrite = typer.confirm(
            f"Config already exists at {CONFIG_PATH}. Overwrite?"
        )
        if not overwrite:
            raise typer.Exit()

    console.print(f"[bold]MemSync v{__version__} — init[/bold]\n")

    # Device
    device_id = uuid.uuid4().hex[:8]
    device_name = typer.prompt("Device name", default=_default_device_name())

    # Storage backend
    backend_type = typer.prompt(
        "Storage backend",
        type=typer.Choice(["local", "s3"]),
        default="local",
    )

    config: dict = {
        "device": {"id": device_id, "name": device_name},
        "storage": {"backend": backend_type},
        "sync": {"claude_dir": "~/.claude", "max_file_size": 52_428_800},
        "crypto": {"argon2_memory_kb": 65_536},
    }

    if backend_type == "local":
        default_path = "~/Dropbox/memsync"
        path = typer.prompt("Storage folder path", default=default_path)
        config["storage"]["path"] = path
        # Create directory
        full_path = Path(path).expanduser()
        full_path.mkdir(parents=True, exist_ok=True)
        console.print(f"  Storage: {full_path}")
    else:
        bucket = typer.prompt("S3 bucket name", default="memsync")
        region = typer.prompt("AWS region", default="us-east-1")
        endpoint = typer.prompt("Endpoint URL (blank for AWS)", default="")
        config["storage"]["bucket"] = bucket
        config["storage"]["region"] = region
        if endpoint:
            config["storage"]["endpoint_url"] = endpoint
        console.print(f"  Storage: s3://{bucket}/")

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
            f"Set {passphrase} environment variable MEMSYNC_PASSPHRASE instead."
        )

    # Save config
    save_config(config)
    console.print(f"  Config written to {CONFIG_PATH}")

    # Register device
    backend = get_backend(load_config())
    register_device(backend, device_id, device_name)
    console.print(f"  Device registered: {device_name} ({device_id})")

    console.print("\n[green]✓ MemSync initialized. Run 'msync push' to sync.[/green]")


# ── push ──────────────────────────────────────────────────────────────


@app.command()
def push(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed output"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change"),
) -> None:
    """Push local session data to storage."""
    config = _get_config()
    passphrase = _get_passphrase_or_exit(config)
    memory_kb = config["crypto"]["argon2_memory_kb"]

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    try:
        _do_push(config, passphrase, memory_kb, verbose, dry_run)
    finally:
        release_lock()


def _do_push(
    config: dict,
    passphrase: str,
    memory_kb: int,
    verbose: bool,
    dry_run: bool,
) -> None:
    start = time.time()
    device_id = config["device"]["id"]
    device_name = config["device"]["name"]
    claude_dir = config["sync"]["claude_dir"]
    max_file_size = config["sync"]["max_file_size"]

    backend = get_backend(config)

    # Build local manifest
    skipped: list[tuple[str, str]] = []

    def on_skip(path: str, reason: str) -> None:
        skipped.append((path, reason))
        if verbose:
            console.print(f"  [dim]skipped: {path} ({reason})[/dim]")

    console.print("[bold]Building manifest...[/bold]")
    local_manifest = build_manifest(
        device_id, device_name, claude_dir, max_file_size, on_skip
    )
    file_count = len(local_manifest["files"])
    console.print(f"  {file_count} files scanned")

    if skipped:
        console.print(f"  [yellow]{len(skipped)} files skipped[/yellow]")

    # Fetch remote manifest
    remote_manifest = _fetch_remote_manifest(backend, device_id, passphrase, memory_kb)

    # Diff
    diff = diff_manifests(local_manifest, remote_manifest)

    if dry_run:
        console.print("\n[bold]Dry run — no changes made:[/bold]")
        _print_diff_summary(diff, time.time() - start)
        return

    if not diff.has_changes:
        console.print("[green]Nothing to push — everything is up to date.[/green]")
        return

    # Upload changed blobs
    to_upload = {**diff.new, **diff.modified}
    console.print(f"\n[bold]Uploading {len(to_upload)} files...[/bold]")
    bytes_transferred = 0

    claude_path = Path(claude_dir).expanduser().resolve()
    for rel_path, info in to_upload.items():
        file_path = claude_path / rel_path
        if not file_path.exists():
            if verbose:
                console.print(f"  [dim]skipped (missing): {rel_path}[/dim]")
            continue

        data = file_path.read_bytes()
        enc_data = encrypt(data, passphrase, memory_kb)
        blob_key = f"data/{device_id}/{info['sha256']}.enc"
        backend.put(blob_key, enc_data)
        bytes_transferred += len(enc_data)

        if verbose:
            console.print(f"  [green]↑[/green] {rel_path}")

    # Upload manifest
    manifest_data = serialize_manifest(local_manifest)
    enc_manifest = encrypt(manifest_data, passphrase, memory_kb)
    manifest_key = f"manifests/{device_id}/manifest.json.enc"
    backend.put(manifest_key, enc_manifest)

    # Update device last_seen
    update_last_seen(backend, device_id)

    elapsed = time.time() - start
    console.print(f"\n[bold green]Push complete.[/bold green]")
    _print_diff_summary(diff, elapsed)
    if bytes_transferred:
        mb = bytes_transferred / (1024 * 1024)
        console.print(f"  {mb:.1f}MB transferred")


# ── pull ──────────────────────────────────────────────────────────────


@app.command()
def pull(
    from_device: Optional[str] = typer.Option(
        None, "--from", help="Pull from a specific device ID"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Pull session data from storage to local."""
    config = _get_config()
    passphrase = _get_passphrase_or_exit(config)
    memory_kb = config["crypto"]["argon2_memory_kb"]

    try:
        acquire_lock()
    except LockError as e:
        _error(str(e))

    try:
        _do_pull(config, passphrase, memory_kb, from_device, verbose, dry_run)
    finally:
        release_lock()


def _do_pull(
    config: dict,
    passphrase: str,
    memory_kb: int,
    from_device: str | None,
    verbose: bool,
    dry_run: bool,
) -> None:
    start = time.time()
    my_device_id = config["device"]["id"]
    claude_dir = config["sync"]["claude_dir"]
    path_map = config.get("sync", {}).get("path_map", {})

    backend = get_backend(config)

    # Find devices to pull from
    devices = list_devices(backend)
    if from_device:
        devices = [d for d in devices if d["device_id"] == from_device]
        if not devices:
            _error(f"Device not found: {from_device}")
    else:
        # Pull from all other devices
        devices = [d for d in devices if d["device_id"] != my_device_id]

    if not devices:
        console.print("[yellow]No other devices found to pull from.[/yellow]")
        return

    claude_path = Path(claude_dir).expanduser().resolve()
    total_new = 0
    total_modified = 0
    total_deleted = 0
    bytes_transferred = 0

    for device in devices:
        did = device["device_id"]
        dname = device["device_name"]
        console.print(f"\n[bold]Pulling from {dname} ({did})...[/bold]")

        remote_manifest = _fetch_remote_manifest(backend, did, passphrase, memory_kb)
        if remote_manifest is None:
            console.print(f"  [yellow]No manifest for {dname}[/yellow]")
            continue

        # Rewrite paths if needed
        remote_files = remote_manifest.get("files", {})
        if path_map:
            remote_files = rewrite_manifest_paths(remote_files, path_map)

        # Build a pseudo-local manifest to diff against
        local_files: dict[str, dict] = {}
        for rel_path in remote_files:
            local_path = claude_path / rel_path
            if local_path.exists():
                from memsync.manifest import hash_file

                try:
                    sha = hash_file(local_path)
                    local_files[rel_path] = {"sha256": sha}
                except (PermissionError, OSError):
                    pass

        # Diff: remote is source of truth
        diff = diff_manifests(
            {"files": remote_files},
            {"files": local_files},
        )

        if dry_run:
            console.print(f"  Dry run for {dname}:")
            _print_diff_summary(diff, 0)
            continue

        if not diff.has_changes:
            console.print(f"  [green]Up to date with {dname}.[/green]")
            continue

        # Download new and modified files
        to_download = {**diff.new, **diff.modified}
        for rel_path, info in to_download.items():
            blob_key = f"data/{did}/{info['sha256']}.enc"
            try:
                enc_data = backend.get(blob_key)
            except MemSyncError:
                if verbose:
                    console.print(f"  [yellow]blob missing: {blob_key}[/yellow]")
                continue

            plain_data = decrypt(enc_data, passphrase, memory_kb)
            bytes_transferred += len(enc_data)

            # Atomic write
            local_path = claude_path / rel_path
            local_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
            tmp_path.write_bytes(plain_data)
            tmp_path.rename(local_path)

            if verbose:
                console.print(f"  [green]↓[/green] {rel_path}")

        # Delete files not in remote manifest (truth-based)
        for rel_path in diff.deleted:
            local_path = claude_path / rel_path
            if local_path.exists():
                local_path.unlink()
                if verbose:
                    console.print(f"  [red]✕[/red] {rel_path}")

        total_new += len(diff.new)
        total_modified += len(diff.modified)
        total_deleted += len(diff.deleted)

        # Write sync log so Claude Code knows what changed from this device
        if not dry_run and diff.has_changes:
            logs = write_sync_log(
                claude_dir=claude_dir,
                device_name=dname,
                device_id=did,
                new_files=list(diff.new.keys()),
                modified_files=list(diff.modified.keys()),
                deleted_files=diff.deleted,
            )
            if verbose and logs:
                for log in logs:
                    console.print(f"  [dim]wrote sync log: {log}[/dim]")

    elapsed = time.time() - start
    console.print(f"\n[bold green]Pull complete.[/bold green]")
    console.print(
        f"  + {total_new} new, ~ {total_modified} modified, - {total_deleted} deleted"
    )
    if bytes_transferred:
        mb = bytes_transferred / (1024 * 1024)
        console.print(f"  {mb:.1f}MB transferred")
    console.print(f"  Completed in {elapsed:.1f}s")


# ── status ────────────────────────────────────────────────────────────


@app.command()
def status() -> None:
    """Show sync status: local vs remote state."""
    config = _get_config()
    passphrase = _get_passphrase_or_exit(config)
    memory_kb = config["crypto"]["argon2_memory_kb"]
    device_id = config["device"]["id"]
    device_name = config["device"]["name"]
    claude_dir = config["sync"]["claude_dir"]
    max_file_size = config["sync"]["max_file_size"]

    backend = get_backend(config)

    # Build local manifest
    local_manifest = build_manifest(
        device_id, device_name, claude_dir, max_file_size
    )
    local_count = len(local_manifest["files"])

    # Fetch remote manifest
    remote_manifest = _fetch_remote_manifest(backend, device_id, passphrase, memory_kb)
    remote_count = len(remote_manifest["files"]) if remote_manifest else 0

    # Diff
    diff = diff_manifests(local_manifest, remote_manifest)

    # Devices
    devices = list_devices(backend)

    console.print(f"\n[bold]MemSync Status[/bold]")
    console.print(f"  Device: {device_name} ({device_id})")
    console.print(f"  Local files: {local_count}")
    console.print(f"  Remote files: {remote_count}")

    if diff.has_changes:
        console.print(f"\n  [yellow]Pending push:[/yellow]")
        if diff.new:
            console.print(f"    + {len(diff.new)} new")
        if diff.modified:
            console.print(f"    ~ {len(diff.modified)} modified")
        if diff.deleted:
            console.print(f"    - {len(diff.deleted)} deleted")
    else:
        console.print(f"\n  [green]In sync.[/green]")

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
        marker = "[green]← this device[/green]" if d["device_id"] == my_id else ""
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
) -> None:
    """Show what would change without applying (dry run)."""
    config = _get_config()
    passphrase = _get_passphrase_or_exit(config)
    memory_kb = config["crypto"]["argon2_memory_kb"]
    device_id = config["device"]["id"]
    device_name = config["device"]["name"]
    claude_dir = config["sync"]["claude_dir"]
    max_file_size = config["sync"]["max_file_size"]

    backend = get_backend(config)

    target_id = from_device or device_id
    local_manifest = build_manifest(
        device_id, device_name, claude_dir, max_file_size
    )
    remote_manifest = _fetch_remote_manifest(backend, target_id, passphrase, memory_kb)

    diff = diff_manifests(local_manifest, remote_manifest)

    console.print(f"\n[bold]Diff against {'device ' + target_id if from_device else 'remote'}:[/bold]")
    if not diff.has_changes:
        console.print("[green]No differences.[/green]")
        return

    for path in sorted(diff.new):
        console.print(f"  [green]+ {path}[/green]")
    for path in sorted(diff.modified):
        console.print(f"  [yellow]~ {path}[/yellow]")
    for path in sorted(diff.deleted):
        console.print(f"  [red]- {path}[/red]")


# ── gc ────────────────────────────────────────────────────────────────


@app.command()
def gc(
    dry_run: bool = typer.Option(False, "--dry-run", help="List orphans without deleting"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Garbage collect orphaned blobs not referenced by any manifest."""
    config = _get_config()
    passphrase = _get_passphrase_or_exit(config)
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
) -> None:
    backend = get_backend(config)

    # Collect all referenced hashes from ALL device manifests
    devices = list_devices(backend)
    referenced_hashes: set[str] = set()

    for device in devices:
        did = device["device_id"]
        manifest = _fetch_remote_manifest(backend, did, passphrase, memory_kb)
        if manifest is None:
            continue
        for info in manifest.get("files", {}).values():
            referenced_hashes.add(info["sha256"])

    # List all blobs across all devices
    all_blobs = backend.list_keys("data/")
    orphan_count = 0
    orphan_bytes = 0

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


# ── helpers ───────────────────────────────────────────────────────────


def _default_device_name() -> str:
    """Generate a default device name from hostname."""
    import socket

    return socket.gethostname()


if __name__ == "__main__":
    app()
