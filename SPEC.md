# MemSync

> Open-source, self-hosted CLI tool for syncing Claude Code sessions across Macs via iCloud Drive.

**License:** MIT
**Status:** Pre-release

This is a personal tool. Anyone with a Claude Code setup and a Mac with iCloud Drive should be able to install it, run `msync init`, and be syncing within five minutes — no account creation, no third-party API, no vendor trust required.

## Problem

Claude Code stores sessions, todos, artifacts, and tool results locally in `~/.claude/projects/`. Switch machines and you lose continuity. claude-sync.com solves this but routes your data through a third-party server. This project is an open-source alternative: same workflow, your own storage, zero trust dependencies. Anyone should be able to set it up in minutes.

---

## Goals

1. **Push/pull Claude Code session data** between Macs via iCloud Drive.
2. **Client-side encryption** — iCloud never sees plaintext.
3. **Minimal infra** — no API server, no database. Just the CLI and iCloud Drive.
4. **Fast** — manifest-based diffing with gzip compression; only transfer what changed.
5. **Single binary feel** — Python CLI installed via `pipx`, zero config to start.
6. **Open-source and forkable** — MIT-licensed, clean codebase, no proprietary dependencies.
7. **Newcomer-friendly docs** — a first-time user with no context should go from `pip install` to working sync by following the README alone.

## Non-Goals

- Real-time sync or file watching (explicit push/pull only).
- Multi-user collaboration or sharing sessions between users.
- GUI or web interface.
- Cross-platform support (Mac-only — uses iCloud Drive).

---

## Documentation Philosophy

Every piece of documentation targets one of two audiences:

1. **End users** — people who want to sync their Claude Code sessions. The README and `docs/` folder should hold their hand from install through first successful sync, with copy-pasteable commands and no assumed knowledge beyond "I use Claude Code."
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

Single storage backend: iCloud Drive via the local filesystem. Encrypted blobs are written to `~/Library/Mobile Documents/com~apple~CloudDocs/memsync/` and iCloud handles sync. The CLI supports multiple sync sources (e.g. `~/.claude` and `~/.gstack`) via configurable `[[sync.sources]]` in config.toml.

```
┌─────────────┐         encrypted blobs    ┌──────────────────┐
│  Machine A  │ ────────written to disk────►│  ~/Library/      │
│  (CLI)      │                             │  Mobile Documents│
└─────────────┘                             │  /com~apple~     │
                                            │  CloudDocs/      │
      ▲                                     │  memsync/        │
      │                                     └──────────────────┘
      │                                            │
      │                                     iCloud Drive sync
      │                                            │
┌─────────────┐                                    │
│  Machine B  │  ◄─────────────────────────────────┘
│  (CLI)      │    (folder already synced by iCloud)
└─────────────┘
```

### Module Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    msync CLI (typer)                          │
│  init | push | pull | status | devices | diff | gc | sources │
└──────────┬──────────────────────────────────┬───────────────┘
           │                                  │
     ┌─────▼─────┐    ┌──────────┐    ┌──────▼────────┐
     │ manifest.py│    │ merge.py │    │   crypto.py   │
     │ walk/hash/ │    │ JSONL    │    │ keygen/encrypt│
     │ diff       │    │ merge    │    │ decrypt/gzip  │
     └─────┬─────┘    └─────┬────┘    └──────┬────────┘
           │                │                 │
           │         ┌──────▼───────┐         │
           └────────►│  config.py   │◄────────┘
                     │  devices.py  │
                     │  errors.py   │
                     └──────┬───────┘
                            │
                   ┌────────▼────────┐
                   │  LocalBackend   │
                   │  put/get/list/  │
                   │  delete/exists  │
                   │  (pathlib)      │
                   └─────────────────┘
```

### No API Server

claude-sync.com runs an API that brokers auth, generates presigned URLs, and stores manifests. We skip all of that. The CLI writes encrypted blobs to a local folder synced by iCloud Drive. Manifests live alongside the data.

---

## Data Model

### Storage Layout

```
{storage_root}/                            # iCloud Drive folder
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

Default `storage_root`: `~/Library/Mobile Documents/com~apple~CloudDocs/memsync/`

### Device Config (`~/.config/memsync/config.toml`)

```toml
[device]
id = "a1b2c3d4"           # generated on init
name = "MacBook Pro"       # user-friendly label

[storage]
path = "~/Library/Mobile Documents/com~apple~CloudDocs/memsync"

[sync]
max_file_size = 52428800   # bytes (50MB). Skip files larger than this.

[[sync.sources]]
name = "claude"
path = "~/.claude"
type = "claude"            # uses existing projects/*/memory|todos walker

[[sync.sources]]
name = "gstack"
path = "~/.gstack"
type = "generic"           # whitelist-based walker
include_dirs = ["projects", "analytics", "retros"]
include_files = ["config.yaml", ".completeness-intro-seen", ".telemetry-prompted",
                 ".proactive-prompted", ".welcome-seen", ".codex-desc-healed"]

[crypto]
argon2_memory_kb = 65536   # Argon2id memory parameter in KB (default: 64MB). Lower for constrained environments.
```

**Backward compatibility:** Old configs using `sync.claude_dir` are auto-converted to a single claude source on load.

### Manifest Schema

The manifest is a **truth-based snapshot** of the local filesystem state. It always reflects the complete current state — files present locally are listed, files not present locally are omitted. Deletions propagate naturally: when a file is deleted locally, the next push produces a manifest without it.

**v2 manifests** include both `files` (v1 backward compat) and `sources` (v2 multi-source):

```json
{
  "device_id": "a1b2c3d4",
  "device_name": "MacBook Pro",
  "timestamp": "2026-04-08T12:00:00Z",
  "files": {
    "projects/-Users-kb-myapp/memory/user_role.md": {
      "sha256": "e3b0c44298fc...",
      "size": 4096,
      "mtime": "2026-04-08T11:30:00Z"
    }
  },
  "sources": {
    "claude": {
      "base_path": "/Users/kb/.claude",
      "files": {
        "projects/-Users-kb-myapp/memory/user_role.md": {
          "sha256": "e3b0c44298fc...",
          "size": 4096,
          "mtime": "2026-04-08T11:30:00Z"
        }
      }
    },
    "gstack": {
      "base_path": "/Users/kb/.gstack",
      "files": {
        "projects/myapp/review-log.jsonl": {
          "sha256": "ab12cd34ef56...",
          "size": 2048,
          "mtime": "2026-04-08T10:00:00Z"
        }
      }
    }
  }
}
```

**Backward compatibility:**
- Old code reads `files`, gets claude data, syncs normally (ignores `sources`).
- New code reads `sources`, gets everything.
- New code reading v1 manifests: falls back to `files`, wraps as a claude source.

### End-to-End Encryption

All session data is encrypted on the device before it touches iCloud Drive. iCloud never sees plaintext. This is a hard invariant — the CLI must never write unencrypted session data to storage under any code path.

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
- Derived key cached in the OS keyring via `keyring` library (macOS Keychain)
- **Headless fallback:** if no keyring is available, fall back to `MEMSYNC_PASSPHRASE` environment variable
- **Single function:** `crypto.get_passphrase()` encapsulates the full fallback chain (keyring → env var → prompt). All commands call this — no duplication.
- Passphrase and derived key are never written to disk in plaintext

**Memory constraint:** The entire encrypt/decrypt pipeline operates on bytes in memory (read → gzip → encrypt → upload). Combined with the `max_file_size` cap (default 50MB), peak memory per file is bounded.

**Multi-device key sharing:**
- All devices must use the same passphrase — this is the "end-to-end" part
- When running `msync init` on a second machine, the user enters the same passphrase
- There is no key exchange protocol; the passphrase is the shared secret
- If a user enters the wrong passphrase, decryption fails with a clear error (GCM tag verification), not silent corruption

**Threat model:**
- **iCloud compromise:** attacker sees only encrypted blobs + device metadata. No session content, no file paths (manifest is encrypted too).
- **Lost/stolen device:** attacker needs both filesystem access AND the OS keyring passphrase (or the encryption passphrase itself) to decrypt.
- **Man-in-the-middle:** iCloud uses TLS for cloud sync. Even if transport is compromised, data is encrypted at rest.
- **NOT in scope:** protecting against a compromised device with active malware that can read process memory. If your machine is owned, your Claude sessions are already exposed locally in `~/.claude/`.

### Storage Interface

`LocalBackend` provides the storage interface. All CLI logic (`push`, `pull`, `status`) calls these methods, never pathlib directly.

```python
class LocalBackend:
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def list_keys(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
```

- Keys map to file paths relative to the configured root folder.
- Atomic writes via temp file + `os.rename`.
- Detects and resolves iCloud and Dropbox-style conflicted copies automatically (see Conflict Resolution).

Factory function `get_backend(config) → LocalBackend` reads `config.storage.path` and returns the backend.

---

## CLI Interface

Built with `typer`. Installed as `msync` (MemSync).

### Commands

```
msync init                     # generate device ID, configure storage, set passphrase
msync push                     # build manifest, diff against remote, upload changes
msync pull [--from DEVICE] [--source NAME]   # download changes (optionally scoped)
msync status [--source NAME]   # show local vs remote state, pending changes
msync devices                  # list registered devices
msync diff [--from DEVICE] [--source NAME]   # show what would change (dry run)
msync gc                       # delete orphaned blobs not referenced by any manifest
msync sources                  # list configured sync sources with status
```

### Global Flags

```
--verbose                      # show each file hashed, each blob transferred, timing info
--dry-run                      # show what would happen without doing it
```

### `msync init`

1. Detect if already initialized. If config.toml exists, ask to overwrite.
2. Generate a UUID4 device ID.
3. Prompt for device name.
4. Prompt for storage folder path (default: iCloud Drive `~/Library/Mobile Documents/com~apple~CloudDocs/memsync`). Create it if it doesn't exist.
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
8. Write encrypted blobs to `data/{device_id}/{sha256}.enc`.
9. Write encrypted manifest to `manifests/{device_id}/manifest.json.enc`.
10. Release lockfile.
11. Print summary (files scanned, changed, bytes transferred, time elapsed).

### `msync pull [--from DEVICE] [--source NAME]`

1. Acquire lockfile.
2. List devices from `devices/` prefix.
3. If `--from` specified, pull only that device's manifest. Otherwise pull all.
4. Download + decrypt remote manifest(s). Handle iCloud conflict resolution if applicable.
5. If `--source` specified, diff only that source. Otherwise diff all sources.
6. Download + decrypt changed blobs.
7. Decompress (gzip) decrypted data.
8. For `.jsonl` files, merge instead of overwrite (union of lines, dedup, sort by `ts`).
9. Write files to their respective source paths using atomic writes (write to `.tmp`, then `os.rename`).
10. Delete local files absent from the remote manifest, scoped to selected source(s).
11. Write `.memsync-log.md` per affected project (claude source only).
12. Release lockfile.
13. Print summary.

### `msync gc`

1. Acquire lockfile.
2. Download and decrypt ALL device manifests.
3. Collect the set of all SHA-256 hashes referenced by any manifest.
4. List all blobs in `data/` across all devices.
5. Delete blobs not in the referenced set.
6. Supports `--dry-run` (list what would be deleted without deleting).
7. Release lockfile.
8. Print summary (blobs deleted, bytes freed).

### `msync status [--source NAME]`

1. Build local manifest (no upload).
2. Fetch remote manifest for current device.
3. Fetch device list.
4. Print: local file count, remote file count per device, pending pushes, pending pulls.
5. If `--source` is given, show changes for that source only. Otherwise group changes by source.

### `msync sources`

1. Read `[[sync.sources]]` from config.toml.
2. For each source, display: name, type, path, and whether the path exists.
3. For `generic` sources, also show `include_dirs` and `include_files`.

---

## Conflict Resolution

iCloud Drive (and Dropbox, if the storage path points there) can create conflicted copies when two machines write the same file concurrently.

**iCloud conflict pattern:** `filename 2.ext`, `filename 3.ext`
**Dropbox conflict pattern:** `filename (conflicted copy YYYY-MM-DD).ext`

The `LocalBackend` detects both patterns via regex matching.

**Resolution strategy:**

1. On `get()` for manifest files, scan for conflicted copies.
2. If found, attempt to decrypt both the original and each conflicted copy.
3. Keep the one with the newest `timestamp` in the decrypted manifest JSON.
4. Delete the losers.
5. If all copies fail to decrypt, fall back to full re-push.

**Manifest corruption recovery:**

If manifest decryption fails (corrupt data, partial write, wrong format):

1. Log a warning: "Manifest corrupt or unreadable — will perform full re-push."
2. Treat as empty manifest (all local files are "new").
3. Full re-push: upload all blobs and write a fresh manifest.

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

### Source Types and Synced Paths

Each sync source has a type that determines how files are discovered:

**`claude` type** — the original walker. Scans `projects/*/memory/` and `projects/*/todos/` within the source path. Everything else (sessions, settings, etc.) is excluded — sessions are ephemeral conversation transcripts (large, not useful across machines), and settings/CLAUDE.md/agents/commands are already git-tracked.

```python
SYNCED_SUBDIRS = ["memory", "todos"]
```

**`generic` type** — whitelist-based walker. Walks only the directories listed in `include_dirs` (recursively) and includes individual files listed in `include_files` at the source root. Nothing else is synced. This is used for sources like `~/.gstack` where specific directories and config files need syncing but the rest should be ignored.

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

## Multi-Source Sync

MemSync supports syncing multiple data sources beyond `~/.claude`. Each source is defined in `[[sync.sources]]` in config.toml.

### Source Types

- **`claude`** — The original walker. Scans `projects/*/memory/` and `projects/*/todos/`. One claude source is always present.
- **`generic`** — Whitelist-based walker. Walks only `include_dirs` recursively and picks up `include_files` at the source root. Used for `~/.gstack` and other structured data directories.

### JSONL Merge on Pull

`.jsonl` files (common in gstack for review logs, analytics, etc.) use a merge strategy instead of overwrite on pull:

1. Union of lines from local and remote (byte-exact dedup after whitespace strip).
2. Sort by `ts` field if present in JSON lines.
3. Lexicographic tiebreaker for non-JSON or timestamp-less lines.

The merged result becomes the new local truth and propagates on the next push.

### Backward Compatibility

v2 manifests include both `files` (containing only claude source files) and `sources` (containing all sources). This allows:

- **Old clients** to read `files` and sync claude data normally, ignoring `sources`.
- **New clients** to read `sources` for full multi-source sync.
- **New clients reading v1 manifests** to fall back to `files` and wrap it as a claude source.

Old configs using `sync.claude_dir` are auto-converted to a single claude source on load.

### Auto-Detection

`msync init` auto-detects known sources (e.g. if `~/.gstack` exists, it is added as a gstack source with default include patterns). Users can add or modify sources in config.toml.

### Source-Scoped Operations

- **Push** always pushes all sources. No `--source` flag on push.
- **Pull, status, diff** accept an optional `--source NAME` flag to operate on a single source for troubleshooting.
- **Deletions** during pull are scoped to the selected source.
- **Sync logs** are written only for the claude source. Generic sources don't produce sync logs.
- **GC** iterates `sources.*.files` in all manifests (with a backward-compat shim for v1 manifests).

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
- `push: failed to write blob abc123.enc — disk full. Free space on iCloud Drive.`
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
```

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
│   ├── encryption.md          # how encryption works, threat model, key management
│   └── troubleshooting.md     # common issues and fixes
├── src/
│   └── memsync/
│       ├── __init__.py
│       ├── cli.py             # typer app, command definitions
│       ├── manifest.py        # directory walking, hashing, diffing
│       ├── crypto.py          # AES-256-GCM encrypt/decrypt, Argon2id key derivation, gzip
│       ├── errors.py          # MemSyncError hierarchy
│       ├── storage/
│       │   ├── __init__.py    # exports get_backend(config) → LocalBackend
│       │   └── local.py       # Local folder implementation (pathlib) + iCloud conflict resolution
│       ├── devices.py         # device registration, listing
│       ├── lockfile.py        # PID-based concurrency lock
│       ├── synclog.py         # sync log generation
│       ├── merge.py           # JSONL merge logic (union, dedup, sort by ts)
│       └── config.py          # config.toml read/write
└── tests/
    ├── test_manifest.py
    ├── test_crypto.py         # encrypt/decrypt, version byte, compression, passphrase fallback
    ├── test_config.py         # TOML load/save, validation, missing fields
    ├── test_storage_local.py  # uses tmp_path fixture, iCloud + Dropbox conflict resolution
    ├── test_lockfile.py       # acquire/release, stale PID, already held
    └── test_integration.py    # full push→pull round-trip, deletion propagation, GC safety
```

---

## Implementation Order

### Phase 1 — Core (MVP)

1. `errors.py` — error hierarchy
2. `config.py` — read/write config.toml, validate schema
3. `crypto.py` — Argon2id keygen, gzip compress, AES-256-GCM encrypt/decrypt (versioned format)
4. `manifest.py` — walk dir (with excludes + file cap), hash files, diff two manifests
5. `storage/local.py` — local folder implementation (pathlib, atomic writes, iCloud conflict detection)
6. `devices.py` — register, list devices
7. `cli.py` — wire up `init`, `push`, `status` (with lockfile)
8. Tests for each module

**Exit criteria:** can `msync init` + `msync push` from one machine, see encrypted blobs in iCloud Drive.

### Phase 2 — Pull + Multi-Device + GC

9. `cli.py` — add `pull` (with atomic writes + deletion propagation), `devices`, `diff`
10. `cli.py` — add `gc` command (with manifest cross-check safety)
11. `synclog.py` — write `.memsync-log.md` per project after pull
12. Integration test: push from device A, pull from device B, round-trip verified

**Exit criteria:** round-trip sync between two Macs works. GC safely cleans orphaned blobs.

### Phase 3 — Documentation & Developer Experience

13. README with badges, install instructions, quickstart, and usage examples
14. `docs/quickstart.md` — zero-to-syncing walkthrough (end-to-end, assumes nothing)
15. `docs/encryption.md` — how encryption works in plain language, threat model, key rotation
16. `docs/troubleshooting.md` — common failure modes and fixes
17. Inline `--help` text on every command and flag (typer docstrings)
18. CONTRIBUTING.md — dev setup, test commands, PR guidelines, code style

**Exit criteria:** a stranger can clone the repo, read the README, and have a working sync loop without asking questions.

### Phase 4 — Polish & Release

19. `rich` progress bars for upload/download
20. `--verbose` flag on all commands
21. Bandwidth reporting (bytes transferred, time elapsed)
22. `msync nuke --device DEVICE` to remove a device's data
23. CHANGELOG.md with initial release notes
24. PyPI package publishing via `pyproject.toml` (installable via `pip install memsync`)
25. GitHub repo setup: LICENSE, issue templates, CI (pytest + linting via GitHub Actions)

---

## Open Questions (Decide During Build)

1. ~~**Deletion propagation:**~~ **RESOLVED.** Truth-based manifests — push reflects current local state, deletions propagate automatically via push/pull/gc.
2. **Manifest conflict:** two devices push simultaneously. Last-write-wins is fine for v1 since manifests are per-device, but note this.
3. ~~**Large files:**~~ **RESOLVED.** 50MB default cap, configurable via `sync.max_file_size`.
4. ~~**Compression:**~~ **RESOLVED.** Gzip before encrypt. Session JSONs compress 5-10x.
5. ~~**PyPI name:**~~ **RESOLVED.** `memsync`.
6. ~~**CLI command name:**~~ **RESOLVED.** `msync`.
7. **GitHub org:** personal repo or create a dedicated org for discoverability?
