# MemSync

> Open-source, self-hosted CLI tool for syncing Claude Code sessions across machines.

**License:** MIT
**Status:** Pre-release

This is a community tool. Anyone with a Claude Code setup and either an S3-compatible bucket or a synced folder (Dropbox, Google Drive, iCloud Drive, etc.) should be able to install it, run `msync init`, and be syncing within five minutes — no account creation, no third-party API, no vendor trust required.

## Problem

Claude Code stores sessions, todos, artifacts, and tool results locally in `~/.claude/projects/`. Switch machines and you lose continuity. claude-sync.com solves this but routes your data through a third-party server. This project is an open-source alternative: same workflow, your own storage, zero trust dependencies. Anyone should be able to set it up in minutes.

---

## Goals

1. **Push/pull Claude Code session data** between machines via pluggable storage backends.
2. **Two storage backends out of the box** — S3-compatible (AWS S3, Cloudflare R2, MinIO) or local folder-based (Dropbox, Google Drive, iCloud Drive, any synced directory).
3. **Client-side encryption** — the storage backend never sees plaintext.
4. **Minimal infra** — no API server, no database. Just the CLI and a place to put files.
5. **Fast** — manifest-based diffing with gzip compression; only transfer what changed.
6. **Single binary feel** — Python CLI installed via `pipx`, zero config to start.
7. **Open-source and forkable** — MIT-licensed, clean codebase, no proprietary dependencies.
8. **Newcomer-friendly docs** — a first-time user with no context should go from `pip install` to working sync by following the README alone.

## Non-Goals

- Real-time sync or file watching (explicit push/pull only).
- Multi-user collaboration or sharing sessions between users.
- GUI or web interface.
- Conflict resolution beyond last-write-wins (v1).

---

## Documentation Philosophy

Every piece of documentation targets one of two audiences:

1. **End users** — people who want to sync their Claude Code sessions. They may not know what S3 is. The README and `docs/` folder should hold their hand from install through first successful sync, with copy-pasteable commands and no assumed knowledge beyond "I use Claude Code."
2. **Contributors** — developers who want to understand or extend the codebase. CONTRIBUTING.md covers dev setup, test running, and PR expectations. Code should be readable without comments where possible; comments explain *why*, not *what*.

**README structure:**
- One-liner description + badges (PyPI, license, CI)
- 30-second install (`pipx install memsync`)
- 2-minute quickstart (init → push → pull)
- Links to detailed docs
- "How it works" section with the architecture diagram
- Contributing link

---

## Architecture

Two storage backends, identical CLI behavior. The user picks one during `msync init`.

```
                        ┌─ Option A: S3-Compatible ─────────────────┐
                        │                                           │
┌─────────────┐         │  encrypted blobs    ┌──────────────────┐  │
│  Machine A  │ ────────┤  via boto3          │  S3 / R2 / MinIO │  │
│  (CLI)      │         │                     │  (1 bucket)      │  │
└─────────────┘         │                     └──────────────────┘  │
                        ├─ Option B: Local Folder ──────────────────┤
      ▲                 │                                           │
      │                 │  encrypted blobs    ┌──────────────────┐  │
      │                 │  written to disk    │  ~/Dropbox/       │  │
      │                 │                     │    memsync/       │  │
      │                 │                     │  (or GDrive,     │  │
      │                 │                     │   iCloud, etc.)  │  │
      │                 │                     └──────────────────┘  │
      │                 └───────────────────────────────────────────┘
      │                                            │
┌─────────────┐                                    │
│  Machine B  │  ◄─────────────────────────────────┘
│  (CLI)      │    (S3: boto3 pull / Local: folder already synced
└─────────────┘     by Dropbox/GDrive/iCloud/rsync)
```

### Module Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    msync CLI (typer)                      │
│  init | push | pull | status | devices | diff | gc | ... │
└──────────┬──────────────────────────────────┬───────────┘
           │                                  │
     ┌─────▼─────┐                    ┌───────▼────────┐
     │ manifest.py│                    │   crypto.py    │
     │ walk/hash/ │                    │ keygen/encrypt │
     │ diff       │                    │ decrypt/gzip   │
     └─────┬─────┘                    └───────┬────────┘
           │                                  │
           │         ┌──────────────┐         │
           └────────►│  config.py   │◄────────┘
                     │  devices.py  │
                     │  paths.py    │
                     │  errors.py   │
                     └──────┬───────┘
                            │
                   ┌────────▼────────┐
                   │ StorageBackend  │ (ABC)
                   │ put/get/list/   │
                   │ delete/exists   │
                   └───┬─────────┬──┘
                       │         │
              ┌────────▼──┐  ┌──▼─────────┐
              │ LocalBackend│  │ S3Backend  │
              │ (pathlib)  │  │ (boto3)    │
              └────────────┘  └────────────┘
```

### No API Server

claude-sync.com runs an API that brokers auth, generates presigned URLs, and stores manifests. We skip all of that. The CLI talks directly to the storage backend — either via boto3 for S3-compatible services, or by reading/writing files to a local synced folder. Manifests live alongside the data in both cases.

### Backend Comparison

| | S3-Compatible | Local Folder |
|---|---|---|
| **Setup** | Create bucket + IAM credentials | Point at existing synced folder |
| **Transport** | boto3 over HTTPS | Dropbox/GDrive/iCloud handles it |
| **Offline support** | No (needs connectivity to push/pull) | Yes (Dropbox queues and syncs later) |
| **Dependencies** | `boto3` required | No extra dependencies |
| **Best for** | CI/CD, headless servers, teams | Personal multi-machine setups |
| **Conflict handling** | Last-write-wins on manifest | Last-write-wins on manifest (Dropbox may also create conflicted copies of `.enc` files — CLI detects and resolves automatically) |

---

## Data Model

### Storage Layout

Both backends use the same directory structure. The only difference is where the root lives.

```
{storage_root}/                            # S3 bucket or local folder
├── devices/
│   ├── {device_id}.json                   # device metadata (unencrypted)
│   └── ...
├── manifests/
│   ├── {device_id}/
│   │   └── manifest.json.enc             # encrypted manifest
│   └── ...
└── data/
    ├── {device_id}/
    │   ├── {sha256_hash}.enc             # encrypted file blobs (content-addressed)
    │   └── ...
    └── ...
```

- **S3 backend:** `storage_root` = `s3://memsync/`
- **Local backend:** `storage_root` = `~/Dropbox/memsync/` (or any synced folder)

### Device Config (`~/.config/memsync/config.toml`)

**S3 backend:**
```toml
[device]
id = "a1b2c3d4"           # generated on init
name = "MacBook Pro"       # user-friendly label

[storage]
backend = "s3"             # "s3" | "local"
bucket = "memsync"
region = "us-east-1"
endpoint_url = ""          # set for R2 or MinIO

[sync]
claude_dir = "~/.claude"   # override if non-standard
max_file_size = 52428800   # bytes (50MB). Skip files larger than this.

[crypto]
argon2_memory_kb = 65536   # Argon2id memory parameter in KB (default: 64MB). Lower for constrained environments.
```

**Local folder backend (Dropbox, etc.):**
```toml
[device]
id = "a1b2c3d4"
name = "MacBook Pro"

[storage]
backend = "local"
path = "~/Dropbox/memsync"   # any folder synced across machines

[sync]
claude_dir = "~/.claude"
max_file_size = 52428800   # bytes (50MB)

[crypto]
argon2_memory_kb = 65536   # default 64MB, lower for CI/constrained environments
```

### Manifest Schema

The manifest is a **truth-based snapshot** of the local filesystem state. It always reflects the complete current state of `~/.claude/projects/` — files present locally are listed, files not present locally are omitted. Deletions propagate naturally: when a file is deleted locally, the next push produces a manifest without it.

```json
{
  "device_id": "a1b2c3d4",
  "device_name": "MacBook Pro",
  "timestamp": "2026-03-17T12:00:00Z",
  "base_path": "/Users/kb/.claude",
  "files": {
    "projects/-Users-kb-myapp/sessions/abc123.json": {
      "sha256": "e3b0c44298fc...",
      "size": 4096,
      "mtime": "2026-03-17T11:30:00Z"
    }
  }
}
```

### End-to-End Encryption

All session data is encrypted on the device before it touches any storage backend. Neither S3, Dropbox, Google Drive, nor any intermediary ever sees plaintext. This is a hard invariant — the CLI must never write unencrypted session data to the storage backend under any code path.

**What's encrypted:**
- All session files, todos, artifacts, tool results, plans, subagent sessions (everything under `data/`)
- The manifest itself (`manifests/{device_id}/manifest.json.enc`) — file paths and hashes are sensitive

**What's NOT encrypted:**
- `devices/{device_id}.json` — contains only device ID, name, and last-seen timestamp. No session content. Kept in plaintext so any device can discover peers without the passphrase.

**Algorithm:** AES-256-GCM (authenticated encryption — tamper-evident)

**Key derivation:** Argon2id from a user-supplied passphrase (via `argon2-cffi`)
- Argon2id parameters: 3 iterations, 64 MB memory (configurable via `crypto.argon2_memory_kb`), 1 parallelism
- Produces a 256-bit derived key
- Memory parameter is configurable in config.toml for constrained environments (CI, small VPS)

**Per-file format:** `[version:1][salt:16][nonce:12][compressed_ciphertext+tag:*]`
- **Version:** 1 byte — format version (`0x01` for initial release). Enables future format evolution without breaking migrations.
- **Salt:** random 16 bytes, unique per file encryption — stored as bytes 2–17
- **Nonce:** random 12 bytes, unique per file encryption — stored as bytes 18–29
- **Ciphertext + GCM auth tag:** remaining bytes. Plaintext is gzip-compressed before encryption.

Each file gets a fresh random salt and nonce on every encrypt. Same plaintext produces different ciphertext each time. Plaintext is gzip-compressed (default level) before encryption — session JSONs typically compress 5-10x.

**Passphrase handling:**
- Prompted once during `msync init`
- Derived key cached in the OS keyring via `keyring` library (macOS Keychain / GNOME Keyring / Windows Credential Locker)
- **Headless fallback:** if no keyring is available, fall back to `MEMSYNC_PASSPHRASE` environment variable
- **Single function:** `crypto.get_passphrase()` encapsulates the full fallback chain (keyring → env var → prompt). All commands call this — no duplication.
- Passphrase and derived key are never written to disk in plaintext
- `msync rekey` command (Phase 4) to change passphrase and re-encrypt all stored data

**Memory constraint:** The entire encrypt/decrypt pipeline operates on bytes in memory (read → gzip → encrypt → upload). Combined with the `max_file_size` cap (default 50MB), peak memory per file is bounded. Streaming encryption is not needed for v1 but can be added later via a `put_stream` method on the backend ABC.

**Multi-device key sharing:**
- All devices must use the same passphrase — this is the "end-to-end" part
- When running `msync init` on a second machine, the user enters the same passphrase
- There is no key exchange protocol; the passphrase is the shared secret
- If a user enters the wrong passphrase, decryption fails with a clear error (GCM tag verification), not silent corruption

**Threat model:**
- **Storage provider compromise (S3/Dropbox/etc.):** attacker sees only encrypted blobs + device metadata. No session content, no file paths (manifest is encrypted too).
- **Lost/stolen device:** attacker needs both filesystem access AND the OS keyring passphrase (or the encryption passphrase itself) to decrypt.
- **Man-in-the-middle:** S3 uses TLS. Dropbox uses TLS for cloud sync. Even if transport is compromised, data is encrypted at rest.
- **NOT in scope:** protecting against a compromised device with active malware that can read process memory. If your machine is owned, your Claude sessions are already exposed locally in `~/.claude/`.

**Verification command:** `msync verify` (Phase 4) — decrypt a random sample of stored blobs and confirm GCM tags pass, as a sanity check that the passphrase is correct and data is intact.

### Storage Backend Interface

Both backends implement the same ABC. All CLI logic (`push`, `pull`, `status`) is backend-agnostic — it calls the interface, never boto3 or pathlib directly.

```python
class StorageBackend(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...
```

- **`S3Backend`** — wraps `boto3.client('s3')`. Keys map to S3 object keys within the configured bucket.
- **`LocalBackend`** — wraps `pathlib.Path`. Keys map to file paths relative to the configured root folder. The local folder itself is synced by Dropbox / Google Drive / iCloud / rsync — not our problem. Detects and resolves Dropbox-style conflicted copies automatically (see Manifest Corruption Recovery).

Factory function `get_backend(config) → StorageBackend` reads `config.storage.backend` and returns the right implementation.

---

## CLI Interface

Built with `typer`. Installed as `msync` (MemSync).

### Commands

```
msync init                     # generate device ID, configure storage, set passphrase
msync push                     # build manifest, diff against remote, upload changes
msync pull [--from DEVICE]     # download changes from a specific device (or all)
msync status                   # show local vs remote state, pending changes
msync devices                  # list registered devices
msync diff [--from DEVICE]     # show what would change without applying (dry run)
msync gc                       # delete orphaned blobs not referenced by any manifest
msync verify                   # decrypt random sample of stored blobs, confirm integrity
msync rekey                    # change passphrase, re-encrypt all stored data
```

### Global Flags

```
--verbose                      # show each file hashed, each blob transferred, timing info
--debug                        # full stack traces, crypto parameters (never passphrase/key)
--dry-run                      # show what would happen without doing it
```

### `msync init`

1. Detect if already initialized. If config.toml exists, ask to overwrite.
2. Generate a UUID4 device ID.
3. Prompt for device name.
4. Prompt for storage backend: `s3` or `local`.
   - **S3:** prompt for bucket, region, endpoint URL (or detect from `~/.aws/credentials` / env vars). Validate with a test put/get.
   - **Local:** prompt for folder path (default: `~/Dropbox/memsync`). Create it if it doesn't exist.
5. Prompt for encryption passphrase.
6. Store derived key in OS keyring (or instruct to set `MEMSYNC_PASSPHRASE` if no keyring).
7. Write `config.toml`.
8. Write `devices/{device_id}.json` to storage.

### `msync push`

1. Acquire lockfile (`~/.config/memsync/memsync.lock`). Fail if another operation is running.
2. Walk `~/.claude/projects/` recursively.
3. Skip excluded patterns (see below) and files exceeding `max_file_size`.
4. SHA-256 hash each file → build local manifest (truth-based snapshot).
5. Fetch remote manifest for this device from storage (if exists). If corrupt, log warning and treat as empty.
6. Diff: find new, modified, and deleted files.
7. Gzip-compress and encrypt changed files with AES-256-GCM (version byte + salt + nonce + ciphertext).
8. Write encrypted blobs to `data/{device_id}/{sha256}.enc` via storage backend.
9. Write encrypted manifest to `manifests/{device_id}/manifest.json.enc` via storage backend.
10. Release lockfile.
11. Print summary (files scanned, changed, bytes transferred, time elapsed).

### `msync pull [--from DEVICE]`

1. Acquire lockfile.
2. List devices from `devices/` prefix.
3. If `--from` specified, pull only that device's manifest. Otherwise pull all.
4. Download + decrypt remote manifest(s). Handle Dropbox conflict resolution if applicable.
5. Diff against local filesystem.
6. Download + decrypt changed blobs.
7. Decompress (gzip) decrypted data.
8. Write files to local `~/.claude/projects/` using atomic writes (write to `.tmp`, then `os.rename`).
9. Delete local files that are absent from the remote manifest (truth-based: remote manifest is the source of truth).
10. Rewrite path prefixes as needed (see Path Resolution).
11. Release lockfile.
12. Print summary.

### `msync gc`

1. Acquire lockfile.
2. Download and decrypt ALL device manifests.
3. Collect the set of all SHA-256 hashes referenced by any manifest.
4. List all blobs in `data/` across all devices.
5. Delete blobs not in the referenced set.
6. Supports `--dry-run` (list what would be deleted without deleting).
7. Release lockfile.
8. Print summary (blobs deleted, bytes freed).

### `msync status`

1. Build local manifest (no upload).
2. Fetch remote manifest for current device.
3. Fetch device list.
4. Print: local file count, remote file count per device, pending pushes, pending pulls.

---

## Manifest Corruption Recovery

If manifest decryption fails (corrupt data, partial write, wrong format):

1. Log a warning: "Manifest corrupt or unreadable — will perform full re-push."
2. Treat as empty manifest (all local files are "new").
3. Full re-push: upload all blobs and write a fresh manifest.

**Dropbox conflict resolution (LocalBackend only):**

Dropbox creates conflicted copies with a predictable naming pattern (e.g., `manifest.json (conflicted copy 2026-03-18).enc`).

1. On `get()` for manifest files, glob for conflicted copies.
2. If found, attempt to decrypt both the original and each conflicted copy.
3. Keep the one with the newest `timestamp` in the decrypted manifest JSON.
4. Delete the losers.
5. If all copies fail to decrypt, fall back to full re-push.

---

## Concurrency Safety

**Lockfile:** `~/.config/memsync/memsync.lock` — PID-based.

- Acquired at the start of `push`, `pull`, and `gc`.
- Contains the PID of the holding process.
- Stale locks (PID no longer running) are cleaned up automatically.
- If lock is held by a running process, fail with: "Another msync operation is running (PID {pid}). Wait for it to finish or remove ~/.config/memsync/memsync.lock."

**GC safety:** `msync gc` checks ALL device manifests before deleting any blob. A blob is only deleted if it is referenced by zero manifests. This is safe even if another device pushes concurrently — the new blob won't be in any manifest yet, but it also won't be in the delete set (it was just uploaded, not listed during the gc scan).

---

## Excluded Patterns

### Synced Subdirectories

Only these subdirectories within each project are synced:

```python
SYNCED_SUBDIRS = ["memory", "todos"]
```

Everything else under `~/.claude/projects/` (sessions, settings, etc.) is intentionally excluded — sessions are ephemeral conversation transcripts (large, not useful across machines), and settings/CLAUDE.md/agents/commands are already git-tracked.

### Excluded Patterns

Hardcoded, not configurable in v1:

```python
EXCLUDED = [
    "node_modules/",
    ".git/",
    ".DS_Store",
    ".env",
    ".env.*",
    "*.log",
    ".claude-sync/",
    "dist/",
    "build/",
    ".next/",
    ".turbo/",
    "__pycache__/",
    "*.pyc",
    ".memsync-log.md",   # generated by pull, not synced back
]
```

### Sync Log (`.memsync-log.md`)

After `msync pull`, a `.memsync-log.md` file is written to each affected project directory. This gives Claude Code awareness of what changed from other machines:

```markdown
# MemSync Activity

Last pull: 2026-03-18 10:00 UTC from **MacBook Pro** (`abc123`)

## New from other machine
- memory/user_role.md

## Updated from other machine
- memory/feedback_testing.md
```

This file is excluded from sync (listed in EXCLUDED) so it doesn't propagate back. It's a local breadcrumb for Claude Code to discover cross-machine context.

---

## Cross-Machine Path Resolution

Claude Code encodes absolute project paths into its directory structure under `~/.claude/projects/`. For example, `/Users/kb/code/myapp` becomes `~/.claude/projects/-Users-kb-code-myapp/`.

When pulling from a device with a different home directory or OS:

1. Read the `base_path` from the remote manifest.
2. Read the local `base_path`.
3. For each file path in the remote manifest, replace the remote path-encoded prefix with the local equivalent.
4. Example: remote file `projects/-Users-kb-code-myapp/sessions/abc.json` pulled to a Linux box becomes `projects/-home-kb-code-myapp/sessions/abc.json`.

**v1 simplification:** require the user to set an explicit path map in config.toml if home dirs differ:

```toml
[sync.path_map]
"/Users/kb" = "/home/kb"
```

---

## Error Handling

### Error Hierarchy (`memsync/errors.py`)

```python
class MemSyncError(Exception): ...           # base — all msync errors
class CryptoError(MemSyncError): ...         # encryption/decryption failures
class StorageError(MemSyncError): ...        # backend I/O failures
class ConfigError(MemSyncError): ...         # config parsing/validation
class ManifestError(MemSyncError): ...       # manifest corruption/incompatibility
class LockError(MemSyncError): ...           # concurrent operation conflict
```

### Error Message Format

All user-facing errors follow: `[operation]: [what failed] — [why]. [what to do]`

Examples:
- `push: failed to upload blob abc123.enc — S3 access denied. Check your IAM credentials.`
- `pull: failed to decrypt manifest — GCM tag mismatch. Wrong passphrase or corrupt data. Re-push from source device.`
- `push: skipped large-artifact.png (67MB) — exceeds max_file_size (50MB). Increase sync.max_file_size in config.toml to include it.`

---

## Dependencies

**Core (always installed):**
```
typer >= 0.9
cryptography >= 42.0
argon2-cffi >= 23.1
keyring >= 25.0
rich >= 13.0          # pretty terminal output
tomli >= 2.0          # config parsing (stdlib in 3.11+)
```

**Optional (S3 backend):**
```
boto3 >= 1.34        # only needed for S3/R2/MinIO backend
```

Install with: `pip install memsync[s3]`

---

## Project Structure

```
memsync/
├── pyproject.toml
├── LICENSE                    # MIT
├── README.md                  # quickstart, install, usage examples
├── CONTRIBUTING.md            # how to contribute, dev setup, PR guidelines
├── CHANGELOG.md               # release notes
├── CLAUDE.md                  # instructions for Claude Code
├── SPEC.md                    # this file
├── docs/
│   ├── designs/
│   │   └── memsync-v1.md     # design decisions from spec review
│   ├── quickstart.md          # zero-to-syncing walkthrough
│   ├── storage-setup.md       # guides for S3, R2, MinIO, Dropbox, GDrive, iCloud
│   ├── encryption.md          # how encryption works, threat model, key management
│   ├── cross-machine.md       # path resolution, multi-OS tips
│   └── troubleshooting.md     # common issues and fixes
├── src/
│   └── memsync/
│       ├── __init__.py
│       ├── cli.py             # typer app, command definitions
│       ├── manifest.py        # directory walking, hashing, diffing
│       ├── crypto.py          # AES-256-GCM encrypt/decrypt, Argon2id key derivation, gzip
│       ├── errors.py          # MemSyncError hierarchy
│       ├── storage/
│       │   ├── __init__.py    # exports get_backend(config) → StorageBackend
│       │   ├── base.py        # StorageBackend ABC: put, get, list, delete, exists
│       │   ├── s3.py          # S3-compatible implementation (boto3)
│       │   └── local.py       # Local folder implementation (pathlib) + Dropbox conflict resolution
│       ├── devices.py         # device registration, listing
│       ├── paths.py           # cross-machine path resolution
│       └── config.py          # config.toml read/write
└── tests/
    ├── test_manifest.py
    ├── test_crypto.py         # encrypt/decrypt, version byte, compression, passphrase fallback
    ├── test_config.py         # TOML load/save, validation, missing fields
    ├── test_storage_s3.py     # uses moto for S3 mocking
    ├── test_storage_local.py  # uses tmp_path fixture, Dropbox conflict resolution
    ├── test_paths.py
    ├── test_gc.py             # gc safety: never delete referenced blobs
    ├── test_lockfile.py       # acquire/release, stale PID, already held
    └── test_integration.py    # full init→push→pull round-trip, deletion propagation
```

---

## Implementation Order

### Phase 1 — Core (MVP)

1. `errors.py` — error hierarchy
2. `config.py` — read/write config.toml, validate schema
3. `crypto.py` — Argon2id keygen, gzip compress, AES-256-GCM encrypt/decrypt (versioned format)
4. `manifest.py` — walk dir (with excludes + file cap), hash files, diff two manifests
5. `storage/base.py` — `StorageBackend` ABC with `put`, `get`, `list_keys`, `delete`, `exists`
6. `storage/local.py` — local folder implementation (pathlib, atomic writes, Dropbox conflict detection)
7. `storage/s3.py` — S3-compatible implementation (boto3)
8. `devices.py` — register, list devices
9. `cli.py` — wire up `init`, `push`, `status` (with lockfile)
10. Tests for each module

**Exit criteria:** can `msync init` + `msync push` from one machine using either backend, see encrypted blobs in storage.

### Phase 2 — Pull + Multi-Device + GC

11. `paths.py` — path prefix rewriting
12. `cli.py` — add `pull` (with atomic writes + deletion propagation), `devices`, `diff`
13. `cli.py` — add `gc` command (with manifest cross-check safety)
14. Integration test: push from device A, pull from device B, round-trip verified

**Exit criteria:** round-trip sync between two machines works. GC safely cleans orphaned blobs.

### Phase 3 — Documentation & Developer Experience

15. README with badges, install instructions, quickstart, and usage examples
16. `docs/quickstart.md` — zero-to-syncing walkthrough (end-to-end, assumes nothing)
17. `docs/storage-setup.md` — step-by-step guides for AWS S3, Cloudflare R2, self-hosted MinIO, and local folder backends (Dropbox, Google Drive, iCloud Drive)
18. `docs/encryption.md` — how encryption works in plain language, threat model, key rotation
19. `docs/cross-machine.md` — path resolution explained, multi-OS gotchas
20. `docs/troubleshooting.md` — common failure modes and fixes
21. Inline `--help` text on every command and flag (typer docstrings)
22. CONTRIBUTING.md — dev setup, test commands, PR guidelines, code style

**Exit criteria:** a stranger can clone the repo, read the README, and have a working sync loop without asking questions.

### Phase 4 — Polish & Release

23. `rich` progress bars for upload/download
24. `--verbose` and `--debug` flags on all commands
25. Bandwidth reporting (bytes transferred, time elapsed)
26. `msync nuke --device DEVICE` to remove a device's data from the bucket
27. `msync rekey` — change encryption passphrase, re-encrypt all stored data
28. `msync verify` — decrypt a random sample of stored blobs, confirm GCM tags pass
29. CHANGELOG.md with initial release notes
30. PyPI package publishing via `pyproject.toml` (installable via `pip install memsync`)
31. GitHub repo setup: LICENSE, issue templates, CI (pytest + linting via GitHub Actions)

---

## Open Questions (Decide During Build)

1. ~~**Deletion propagation:**~~ **RESOLVED.** Truth-based manifests — push reflects current local state, deletions propagate automatically via push/pull/gc.
2. **Manifest conflict:** two devices push simultaneously. Last-write-wins is fine for v1 since manifests are per-device, but note this.
3. ~~**Large files:**~~ **RESOLVED.** 50MB default cap, configurable via `sync.max_file_size`.
4. ~~**Compression:**~~ **RESOLVED.** Gzip before encrypt. Session JSONs compress 5-10x.
5. ~~**PyPI name:**~~ **RESOLVED.** `memsync`.
6. ~~**CLI command name:**~~ **RESOLVED.** `msync`.
7. **GitHub org:** personal repo or create a dedicated org for discoverability?
