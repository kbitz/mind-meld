# claude-session-sync

> Open-source, self-hosted CLI tool for syncing Claude Code sessions across machines.

**License:** MIT  
**Status:** Pre-release  

This is a community tool. Anyone with a Claude Code setup and either an S3-compatible bucket or a synced folder (Dropbox, Google Drive, iCloud Drive, etc.) should be able to install it, run `css init`, and be syncing within five minutes — no account creation, no third-party API, no vendor trust required.

## Problem

Claude Code stores sessions, todos, artifacts, and tool results locally in `~/.claude/projects/`. Switch machines and you lose continuity. claude-sync.com solves this but routes your data through a third-party server. This project is an open-source alternative: same workflow, your own storage, zero trust dependencies. Anyone should be able to set it up in minutes.

---

## Goals

1. **Push/pull Claude Code session data** between machines via pluggable storage backends.
2. **Two storage backends out of the box** — S3-compatible (AWS S3, Cloudflare R2, MinIO) or local folder-based (Dropbox, Google Drive, iCloud Drive, any synced directory).
3. **Client-side encryption** — the storage backend never sees plaintext.
4. **Minimal infra** — no API server, no database. Just the CLI and a place to put files.
5. **Fast** — manifest-based diffing; only transfer what changed.
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
- 30-second install (`pipx install claude-session-sync`)
- 2-minute quickstart (init → push → pull)
- Links to detailed docs
- "How it works" section with the architecture diagram
- Contributing link

---

## Architecture

Two storage backends, identical CLI behavior. The user picks one during `css init`.

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
      │                 │  written to disk    │  ~/Dropbox/css/  │  │
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
| **Conflict handling** | Last-write-wins on manifest | Last-write-wins on manifest (Dropbox may also create conflicted copies of raw `.enc` files — CLI detects and warns) |

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

- **S3 backend:** `storage_root` = `s3://claude-session-sync/`
- **Local backend:** `storage_root` = `~/Dropbox/claude-session-sync/` (or any synced folder)

### Device Config (`~/.config/claude-session-sync/config.toml`)

**S3 backend:**
```toml
[device]
id = "a1b2c3d4"           # generated on init
name = "MacBook Pro"       # user-friendly label

[storage]
backend = "s3"             # "s3" | "local"
bucket = "claude-session-sync"
region = "us-east-1"
endpoint_url = ""          # set for R2 or MinIO

[sync]
claude_dir = "~/.claude"   # override if non-standard
```

**Local folder backend (Dropbox, etc.):**
```toml
[device]
id = "a1b2c3d4"
name = "MacBook Pro"

[storage]
backend = "local"
path = "~/Dropbox/claude-session-sync"   # any folder synced across machines

[sync]
claude_dir = "~/.claude"
```

### Manifest Schema

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

**Key derivation:** Argon2id from a user-supplied passphrase
- Argon2id parameters: 3 iterations, 64 MB memory, 1 parallelism (tune based on target device speed)
- Produces a 256-bit derived key

**Per-file format:** `[salt:16][nonce:12][ciphertext+tag:*]`
- **Salt:** random 16 bytes, unique per file encryption — stored as first 16 bytes
- **Nonce:** random 12 bytes, unique per file encryption — stored as bytes 17–28
- **Ciphertext + GCM auth tag:** remaining bytes

Each file gets a fresh random salt and nonce on every encrypt. Same plaintext produces different ciphertext each time.

**Passphrase handling:**
- Prompted once during `css init`
- Derived key cached in the OS keyring via `keyring` library (macOS Keychain / GNOME Keyring / Windows Credential Locker)
- Passphrase and derived key are never written to disk in plaintext
- `css rekey` command (Phase 3+) to change passphrase and re-encrypt all stored data

**Multi-device key sharing:**
- All devices must use the same passphrase — this is the "end-to-end" part
- When running `css init` on a second machine, the user enters the same passphrase
- There is no key exchange protocol; the passphrase is the shared secret
- If a user enters the wrong passphrase, decryption fails with a clear error (GCM tag verification), not silent corruption

**Threat model:**
- **Storage provider compromise (S3/Dropbox/etc.):** attacker sees only encrypted blobs + device metadata. No session content, no file paths (manifest is encrypted too).
- **Lost/stolen device:** attacker needs both filesystem access AND the OS keyring passphrase (or the encryption passphrase itself) to decrypt.
- **Man-in-the-middle:** S3 uses TLS. Dropbox uses TLS for cloud sync. Even if transport is compromised, data is encrypted at rest.
- **NOT in scope:** protecting against a compromised device with active malware that can read process memory. If your machine is owned, your Claude sessions are already exposed locally in `~/.claude/`.

**Verification command:** `css verify` (Phase 3+) — decrypt a random sample of stored blobs and confirm GCM tags pass, as a sanity check that the passphrase is correct and data is intact.

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
- **`LocalBackend`** — wraps `pathlib.Path`. Keys map to file paths relative to the configured root folder. The local folder itself is synced by Dropbox / Google Drive / iCloud / rsync — not our problem.

Factory function `get_backend(config) → StorageBackend` reads `config.storage.backend` and returns the right implementation.

---

## CLI Interface

Built with `typer`. Installed as `css` (claude-session-sync).

### Commands

```
css init                     # generate device ID, configure storage, set passphrase
css push                     # build manifest, diff against remote, upload changes
css pull [--from DEVICE]     # download changes from a specific device (or all)
css status                   # show local vs remote state, pending changes
css devices                  # list registered devices
css diff [--from DEVICE]     # show what would change without applying (dry run)
css verify                   # decrypt random sample of stored blobs, confirm integrity
css rekey                    # change passphrase, re-encrypt all stored data
```

### `css init`

1. Generate a UUID4 device ID.
2. Prompt for device name.
3. Prompt for storage backend: `s3` or `local`.
   - **S3:** prompt for bucket, region, endpoint URL (or detect from `~/.aws/credentials` / env vars).
   - **Local:** prompt for folder path (default: `~/Dropbox/claude-session-sync`). Create it if it doesn't exist.
4. Prompt for encryption passphrase.
5. Store derived key in OS keyring.
6. Write `config.toml`.
7. Write `devices/{device_id}.json` to storage.

### `css push`

1. Walk `~/.claude/projects/` recursively.
2. Skip excluded patterns (see below).
3. SHA-256 hash each file → build local manifest.
4. Fetch remote manifest for this device from storage (if exists).
5. Diff: find new, modified, and deleted files.
6. Encrypt changed files with AES-256-GCM.
7. Write encrypted blobs to `data/{device_id}/{sha256}.enc` via storage backend.
8. Write encrypted manifest to `manifests/{device_id}/manifest.json.enc` via storage backend.
9. Print summary.

### `css pull [--from DEVICE]`

1. List devices from `devices/` prefix.
2. If `--from` specified, pull only that device's manifest. Otherwise pull all.
3. Download + decrypt remote manifest(s).
4. Diff against local filesystem.
5. Download + decrypt changed blobs.
6. Write files to local `~/.claude/projects/`.
7. Rewrite symlinks / path prefixes as needed (see Path Resolution).
8. Print summary.

### `css status`

1. Build local manifest (no upload).
2. Fetch remote manifest for current device.
3. Fetch device list.
4. Print: local file count, remote file count per device, pending pushes, pending pulls.

---

## Excluded Patterns

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
]
```

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

## Dependencies

**Core (always installed):**
```
typer >= 0.9
cryptography >= 42.0
keyring >= 25.0
rich >= 13.0          # pretty terminal output
tomli >= 2.0          # config parsing (stdlib in 3.11+)
```

**Optional (S3 backend):**
```
boto3 >= 1.34        # only needed for S3/R2/MinIO backend
```

Install with: `pip install claude-session-sync[s3]`

---

## Project Structure

```
claude-session-sync/
├── pyproject.toml
├── LICENSE                    # MIT
├── README.md                  # quickstart, install, usage examples
├── CONTRIBUTING.md            # how to contribute, dev setup, PR guidelines
├── CHANGELOG.md               # release notes
├── CLAUDE.md                  # instructions for Claude Code
├── SPEC.md                    # this file
├── docs/
│   ├── quickstart.md          # zero-to-syncing walkthrough
│   ├── storage-setup.md       # guides for S3, R2, MinIO, Dropbox, GDrive, iCloud
│   ├── encryption.md          # how encryption works, threat model, key management
│   ├── cross-machine.md       # path resolution, multi-OS tips
│   └── troubleshooting.md     # common issues and fixes
├── src/
│   └── css/
│       ├── __init__.py
│       ├── cli.py             # typer app, command definitions
│       ├── manifest.py        # directory walking, hashing, diffing
│       ├── crypto.py          # AES-256-GCM encrypt/decrypt, key derivation
│       ├── storage/
│       │   ├── __init__.py    # exports get_backend(config) → StorageBackend
│       │   ├── base.py        # StorageBackend ABC: put, get, list, delete
│       │   ├── s3.py          # S3-compatible implementation (boto3)
│       │   └── local.py       # Local folder implementation (pathlib)
│       ├── devices.py         # device registration, listing
│       ├── paths.py           # cross-machine path resolution
│       └── config.py          # config.toml read/write
└── tests/
    ├── test_manifest.py
    ├── test_crypto.py
    ├── test_storage_s3.py     # uses moto for S3 mocking
    ├── test_storage_local.py  # uses tmp_path fixture
    └── test_paths.py
```

---

## Implementation Order

### Phase 1 — Core (MVP)

1. `config.py` — read/write config.toml
2. `crypto.py` — keygen, encrypt, decrypt
3. `manifest.py` — walk dir, hash, diff two manifests
4. `storage/base.py` — `StorageBackend` ABC with `put`, `get`, `list_keys`, `delete`
5. `storage/local.py` — local folder implementation (pathlib, zero dependencies)
6. `storage/s3.py` — S3-compatible implementation (boto3)
7. `cli.py` — wire up `init`, `push`, `status`
8. Tests for each module

**Exit criteria:** can `css init` + `css push` from one machine using either backend, see encrypted blobs in storage.

### Phase 2 — Pull + Multi-Device

7. `devices.py` — register, list devices
8. `cli.py` — add `pull`, `devices`, `diff`
9. `paths.py` — path prefix rewriting
10. Integration test: push from device A, pull from device B

**Exit criteria:** round-trip sync between two machines works.

### Phase 3 — Documentation & Developer Experience

11. README with badges, install instructions, quickstart, and usage examples
12. `docs/quickstart.md` — zero-to-syncing walkthrough (end-to-end, assumes nothing)
13. `docs/storage-setup.md` — step-by-step guides for AWS S3, Cloudflare R2, self-hosted MinIO, and local folder backends (Dropbox, Google Drive, iCloud Drive)
14. `docs/encryption.md` — how encryption works in plain language, threat model, key rotation
15. `docs/cross-machine.md` — path resolution explained, multi-OS gotchas
16. `docs/troubleshooting.md` — common failure modes and fixes
17. Inline `--help` text on every command and flag (typer docstrings)
18. CONTRIBUTING.md — dev setup, test commands, PR guidelines, code style

**Exit criteria:** a stranger can clone the repo, read the README, and have a working sync loop without asking questions.

### Phase 4 — Polish & Release

19. `rich` progress bars for upload/download
20. `--dry-run` and `--verbose` flags on all commands
21. Bandwidth reporting (bytes transferred, time elapsed)
22. `css nuke --device DEVICE` to remove a device's data from the bucket
23. `css rekey` — change encryption passphrase, re-encrypt all stored data
24. `css verify` — decrypt a random sample of stored blobs, confirm GCM tags pass (data integrity + passphrase correctness check)
25. CHANGELOG.md with initial release notes
26. PyPI package publishing via `pyproject.toml` (installable via `pip install claude-session-sync`)
27. GitHub repo setup: LICENSE, issue templates, CI (pytest + linting via GitHub Actions)

---

## Open Questions (Decide During Build)

1. **Deletion propagation:** if a session is deleted locally, should `push` delete it remotely? v1 proposal: no, push is additive only. Add `css prune` later.
2. **Manifest conflict:** two devices push simultaneously. Last-write-wins is fine for v1 since manifests are per-device, but note this.
3. **Large files:** Claude Code artifacts can include images or large outputs. Cap at 50MB per file? Warn and skip?
4. **Compression:** gzip before encrypt? Probably yes — session JSONs compress well. Decide based on benchmarks.
5. **PyPI name:** `claude-session-sync` is descriptive but long. Alternatives: `claude-sync-oss`, `ccsync`. Check availability.
6. **CLI command name:** `css` is short but collides with CSS in shell history. Alternatives: `csync`, `ccsync`, `sesync`.
7. **GitHub org:** personal repo or create a dedicated org for discoverability?


