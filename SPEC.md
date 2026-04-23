# Mind Meld

> Open-source, self-hosted CLI tool for syncing Claude Code sessions across Macs via iCloud Drive.

**License:** MIT
**Status:** Pre-release

This is a personal tool. Anyone with a Claude Code setup and a Mac with iCloud Drive should be able to install it, run `mm init`, and be syncing within five minutes — no account creation, no third-party API, no vendor trust required.

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
- 30-second install (`pipx install mind-meld`)
- 2-minute quickstart (init → push → pull)
- Links to detailed docs
- "How it works" section with the architecture diagram
- Contributing link

---

## Architecture

Single storage backend: iCloud Drive via the local filesystem. Encrypted blobs are written to `~/Library/Mobile Documents/com~apple~CloudDocs/mind-meld/` and iCloud handles sync. The CLI supports multiple sync sources (e.g. `~/.claude` and `~/.gstack`) via configurable `[[sync.sources]]` in config.toml.

```
┌─────────────┐         encrypted blobs    ┌──────────────────┐
│  Machine A  │ ────────written to disk────►│  ~/Library/      │
│  (CLI)      │                             │  Mobile Documents│
└─────────────┘                             │  /com~apple~     │
                                            │  CloudDocs/      │
      ▲                                     │  mind-meld/        │
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
│                    mm CLI (typer)                          │
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

Default `storage_root`: `~/Library/Mobile Documents/com~apple~CloudDocs/mind-meld/`

### Device Config (`~/.config/mind-meld/config.toml`)

```toml
[device]
id = "a1b2c3d4"           # generated on init
name = "MacBook Pro"       # user-friendly label

[storage]
path = "~/Library/Mobile Documents/com~apple~CloudDocs/mind-meld"

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

**Key derivation (v2, v0.6.0+):** two-level — Argon2id → master_key → HKDF per file.

1. **Root salt + keycheck are stored at the storage root** in an unencrypted
   file named `mm-crypto-init`. Layout:
   `[version=0x02:1][argon2_memory_kb:4 BE][root_salt:16][keycheck_blob:*]`.
   `keycheck_blob` is itself a v2 blob containing the known plaintext
   `b"mm-keycheck-v1"` encrypted under the master_key derived from the
   first-device passphrase.
2. **Per process (once, cached in memory):**
   `master_key = Argon2id(passphrase, root_salt, time=3, memory=argon2_memory_kb, parallelism=1)` → 32 bytes.
3. **Per file (microseconds):**
   `file_key = HKDF-SHA256(master_key, salt=per_file_salt, info=b"mm-file-v2", L=32)`.
4. **Encrypt:** AES-256-GCM with random per-file nonce over the gzip-compressed
   plaintext.

`argon2_memory_kb` lives in `mm-crypto-init`, not per-device config. All
devices must derive master_key with the same value; storing it with the salt
eliminates silent cross-device drift. Local config's `[crypto].argon2_memory_kb`
is a seed used only during first-device bootstrap.

**Per-file blob format (v2):** `[version=0x02:1][salt:16][nonce:12][compressed_ciphertext+tag:*]`
- **Version:** 1 byte — `0x02` for the v0.6 format. v1 (`0x01`) is recognized
  and rejected with a clear error; Mind Meld is pre-release and has no v1
  blobs in the wild.
- **Salt:** random 16 bytes, unique per file — input to HKDF (NOT to Argon2).
- **Nonce:** random 12 bytes, unique per file.
- **Ciphertext + GCM auth tag:** remaining bytes. Plaintext is gzip-compressed
  before encryption.

Each file gets a fresh random salt and nonce on every encrypt. Same plaintext
produces different ciphertext each time. Plaintext is gzip-compressed (default
level) before encryption — session JSONs typically compress 5-10x.

**iCloud convergence for mm-crypto-init.** If two devices run `mm init` nearly
simultaneously, iCloud can leave both devices with different local blobs that
reconcile later by renaming one to `mm-crypto-init 2`. `fetch_crypto_init` (run
at the start of every command) scans for conflict copies, picks the
deterministic winner (lex-smallest `root_salt`), canonicalizes it via
`os.rename`, and deletes the losers. All devices converge on the same winner
as iCloud replicates the reconciled state.

**Cross-process drift detection.** Each device's local config stores
`[crypto].root_salt_fp` — a 16-char hex fingerprint of the `root_salt` it was
initialized against. Every command compares this to the current storage
fingerprint and refuses on mismatch with an actionable error.

**Passphrase handling:**
- First device: `mm init` double-prompts, generates `root_salt` + keycheck, writes
  `mm-crypto-init` atomically (via `os.link` EEXIST).
- Subsequent devices: `mm init` single-prompts, fetches `mm-crypto-init`, decrypts
  keycheck to verify the passphrase matches the first-device's. Wrong passphrase
  aborts cleanly with no local state written (no config, no device registration,
  no keyring write).
- Derived key cached in the OS keyring via `keyring` library (macOS Keychain)
- **Headless fallback:** if no keyring is available, fall back to `MINDMELD_PASSPHRASE` environment variable
- **Single function:** `crypto.get_passphrase()` encapsulates the full fallback chain (keyring → env var → prompt). All commands call this — no duplication.
- Passphrase and derived key are never written to disk in plaintext

**Memory constraint:** The entire encrypt/decrypt pipeline operates on bytes in memory (read → gzip → encrypt → upload). Combined with the `max_file_size` cap (default 50MB), peak memory per file is bounded.

**Multi-device key sharing:**
- All devices must use the same passphrase — this is the "end-to-end" part
- When running `mm init` on a second machine, the user enters the same passphrase
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

Built with `typer`. Installed as `mm` (Mind Meld).

### Commands

```
mm init                     # generate device ID, configure storage, set passphrase
mm push                     # build manifest, diff against remote, upload changes
mm pull [--from DEVICE] [--source NAME]              # download changes (optionally scoped)
           [--resolve-interactive | --no-prompt]        # conflict handling mode
mm status [--source NAME]   # show local vs remote state, pending changes
mm devices                  # list registered devices
mm diff [--from DEVICE] [--source NAME]   # show what would change (dry run)
                                             # annotates modified files as write / merge / skip / conflict
mm gc [--conflicts]         # delete orphaned blobs; with --conflicts, also reap .sync-conflict-* files >30d
mm sources                  # list configured sync sources with status
mm conflicts                # list unresolved .sync-conflict-* files across sources
mm resolve [PATH]           # interactively resolve conflict files (unified diff + pick winner)
mm autopull                 # silent pull for Claude Code (one-line output, never prompts)
mm autopush                 # silent push for Claude Code (one-line output, never prompts)
```

### Global Flags

```
--verbose                      # show each file hashed, each blob transferred, timing info
--dry-run                      # show what would happen without doing it
```

### `mm init`

1. Detect if already initialized. If config.toml exists, ask to overwrite.
2. Generate a UUID4 device ID.
3. Prompt for device name.
4. Prompt for storage folder path (default: iCloud Drive `~/Library/Mobile Documents/com~apple~CloudDocs/mind-meld`). Create it if it doesn't exist.
5. Prompt for encryption passphrase.
6. Store derived key in OS keyring (or instruct to set `MINDMELD_PASSPHRASE` if no keyring).
7. Write `config.toml`.
8. Write `devices/{device_id}.json` to storage.

### `mm push`

1. Acquire lockfile (`~/.config/mind-meld/mind-meld.lock`). Fail if another operation is running.
2. Walk `~/.claude/projects/` recursively.
3. Skip excluded patterns (see below) and files exceeding `max_file_size`.
4. SHA-256 hash each file → build local manifest (truth-based snapshot).
5. Fetch remote manifest for this device from storage via the tri-state `_fetch_remote_manifest` (`ok` / `missing` / `corrupt`). On `corrupt`, run the recovery chain (local sidecar → peer tombstone fallback → refuse; see Manifest corruption recovery). Never silently treat corrupt as empty.
6. Diff: find new, modified, and deleted files.
7. Gzip-compress and encrypt changed files with AES-256-GCM (version byte + salt + nonce + ciphertext).
8. Write encrypted blobs to `data/{device_id}/{sha256}.enc`.
9. Write encrypted manifest to `manifests/{device_id}/manifest.json.enc`.
10. Release lockfile.
11. Print summary (files scanned, changed, bytes transferred, time elapsed).

### `mm pull [--from DEVICE] [--source NAME]`

1. Acquire lockfile.
2. List devices from `devices/` prefix.
3. If `--from` specified, pull only that device's manifest. Otherwise pull all.
4. Download + decrypt remote manifest(s). Handle iCloud/Dropbox conflict resolution if applicable.
5. If `--source` specified, diff only that source. Otherwise diff all sources.
6. Apply tombstones: files in the remote manifest tombstone set are skipped.
7. For each incoming file, re-read the local hash and mtime, then decide per `_apply_incoming_file`: write / update-base / merge / skip (local newer) / conflict-copy. See Conflict Resolution for the full decision tree.
8. Download + decrypt changed blobs. Decompress (gzip).
9. For merge-eligible files (`.jsonl` union-merge, `MEMORY.md` line-merge), merge instead of overwrite.
10. Write files to their respective source paths using atomic writes (write to `.tmp`, then `os.rename`; `.tmp` siblings are cleaned up on failure).
11. For conflict-copy decisions, rename the local file to `<stem>.sync-conflict-<ts>-<device>.<ext>` before writing remote to the canonical path. With `--resolve-interactive`, prompt per-file instead.
12. Pull is **additive-only:** local files absent from the remote manifest are kept. Deletions propagate only via tombstones produced by a subsequent push from the originating device.
13. Write `.mind-meld-log.md` per affected project (claude source only), including `## Conflicts` and `## Skipped (local was newer)` sections when relevant.
14. Release lockfile.
15. Print summary with split counts: written / merged / skipped / conflicted / failed.

### `mm gc`

1. Acquire lockfile.
2. Download and decrypt ALL device manifests.
3. Collect the set of all SHA-256 hashes referenced by any manifest.
4. List all blobs in `data/` across all devices.
5. Delete blobs not in the referenced set.
6. Supports `--dry-run` (list what would be deleted without deleting).
7. Release lockfile.
8. Print summary (blobs deleted, bytes freed).

### `mm status [--source NAME]`

1. Build local manifest (no upload).
2. Fetch remote manifest for current device.
3. Fetch device list.
4. Print: local file count, remote file count per device, pending pushes, pending pulls.
5. If `--source` is given, show changes for that source only. Otherwise group changes by source.

### `mm sources`

1. Read `[[sync.sources]]` from config.toml.
2. For each source, display: name, type, path, and whether the path exists.
3. For `generic` sources, also show `include_dirs` and `include_files`.

---

## Conflict Resolution

There are two distinct conflict surfaces: **manifest-level conflicts** created by the iCloud/Dropbox sync layer, and **source-file conflicts** where the same logical file was edited on two machines before a sync round-trip.

### Manifest conflicts (iCloud / Dropbox)

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

Additive resolution for the multi-manifest case: when multiple conflict copies coexist, the union of referenced files is preserved so no device's entries are dropped before a losing copy is deleted.

**Manifest corruption recovery:**

`_fetch_remote_manifest` returns a tri-state result (`ManifestFetch`):

- `ok` — at least one copy decrypted; caller uses `.manifest`.
- `missing` — no manifest at the expected key (first push or fresh device). Callers that read remote state treat this as "empty remote." Push treats this as the correct "no prior state" signal and writes the first manifest.
- `corrupt` — manifest(s) exist but every copy failed to decrypt/parse. Callers must NOT treat this as "missing"; doing so would drop this device's tombstones and silently un-delete files across the fleet on the next pull.

On `corrupt`, `push` runs the following recovery chain before writing a new manifest:

1. **Local sidecar.** Read `~/.config/mind-meld/last-push.json` (written atomically at the end of every successful push). If present and parseable, use it as the prior-state manifest. This is the only recovery source that preserves **fresh local deletions** — deletions this device made since its last successful push but not yet propagated anywhere.
2. **Peer fallback.** If no sidecar, iterate peer devices and aggregate their tombstones via `collect_tombstones`. This recovers only deletions that previously propagated. Fresh local deletions since the last good push are lost. A warning surfaces this explicitly.
3. **Refuse.** If neither source yields prior state, exit nonzero with an actionable message. Silent empty-tombstone push is never acceptable: it would erase the deletion record across the device fleet.

### Merge invariants (load-bearing)

When `_merge_manifests` combines multiple conflict copies of the same device's manifest, the file-merge and tombstone-merge policies are intentionally asymmetric:

- **Files: UNION across all copies** (older entries survive when absent from newer copies).
- **Tombstones: newest-timestamp-wins** on `deleted_at`.

The asymmetry is correct because the manifest walker is **lossy** — it drops files on permission errors, read failures, and `max_file_size` overruns (see `manifest.walk_claude_source` / `walk_generic_source`). A file missing from the newer conflict copy is **not causal evidence** of deletion; only an explicit tombstone is. Swapping files to newest-wins would silently drop any file that happened to be transiently locked/unreadable during one scan but not another.

**Correctness invariant:** union-for-files + newest-wins-for-tombstones + `is_tombstoned()` gate at every downstream consumer. The tombstone gate is load-bearing. **Every new consumer of a merged manifest MUST check `is_tombstoned(source, rel_path, aggregated_tombstones)` before acting on a file entry.** Adding a consumer that reads `manifest["sources"][x]["files"]` without the gate will silently resurrect deletions.

Current gated consumers: pull-side `_pull_core` (via `collect_tombstones` across all peers, applied at the `to_download` filter step).

### Read-path normalization invariant (load-bearing)

**Every manifest loaded from bytes/disk MUST go through `manifest.load_manifest(bytes) -> dict`.** This single load boundary composes `deserialize_manifest + normalize_manifest` plus full inner-shape validation: `sources` and `tombstones` must be dicts, each source must have a dict `files`, each tombstone value must be a dict. Malformed manifests raise `ManifestError` at the front door instead of crashing downstream consumers (`_merge_manifests`, `collect_tombstones`, `generate_tombstones`, the diff loop) with `AttributeError`.

`_fetch_remote_manifest` already catches `ManifestError` and falls through to the recovery chain (sidecar → peer fallback → refuse), so a malformed peer manifest degrades to a clean `corrupt` status.

`sidecar.read` is the deliberate exception: it uses `deserialize_manifest + structural-shape-check on raw dict + normalize_manifest` (rather than `load_manifest`) so the anti-tampering check on missing `sources`/`tombstones` keys runs against the RAW parsed dict, before `normalize_manifest` would synthesize them.

DO NOT add a new manifest-load path that bypasses `load_manifest` (or sidecar's deliberate variant). The 6 previously-scattered `normalize_manifest(remote_manifest)` calls in `cli.py` were removed in v0.6.0 once this invariant became load-time-guaranteed.

### Source-file conflicts (Syncthing-style conflict-copy preservation)

If the local file has been edited independently of the remote version (local hash ≠ last-synced hash AND local hash ≠ remote hash), pull never destroys local edits. Behavior is decided per-file at apply time by a documented decision tree in `_apply_incoming_file`:

1. **Skip (S):** local mtime is newer than remote mtime — leave local as-is. Convergence happens on the next push.
2. **Merge (M):** file has a mergeable type (`.jsonl` union-merge, `MEMORY.md` line-merge).
3. **Write (W):** no local divergence — write the remote version to the canonical path.
4. **Conflict-copy (C):** local has diverged AND isn't mergeable AND isn't newer — rename local to `<stem>.sync-conflict-<YYYYMMDD-HHMMSS>-<device>.<ext>` (Syncthing convention, collision suffix on clash), then write remote to the canonical path. Local edits are preserved alongside.
5. **Update-base (U):** remote hash matches local hash — no I/O, just refresh the last-synced state.

Pull re-reads local hash and mtime at apply time so the decision reflects the actual state when writing, race-safe against editors running during a long pull.

**Interactive resolution.** `mm pull --resolve-interactive` replaces the default keep-both for conflicts with a per-file prompt (unified diff + pick: canonical / force conflict to canonical / keep both / abort). `--no-prompt` is the explicit script-mode counterpart and is mutually exclusive with `--resolve-interactive`.

**Post-hoc resolution commands:**

- `mm conflicts` — list every `.sync-conflict-*` file across synced sources with age and canonical sibling.
- `mm resolve [PATH]` — walk conflicts (or a single path) interactively. Shows a unified diff, prompts for a winner, acquires the mm lockfile so autopull can't race the rename/unlink. Deletions and renames propagate via the existing additive-sync tombstone machinery.
- `mm gc --conflicts` — reap `.sync-conflict-*` files older than `CONFLICT_AGE_DAYS` (30 days).

**Reporting.** `PullResult` splits into `total_written` / `total_merged` / `total_skipped` / `total_conflicted` / `total_failed`. Pull summary, autopull one-liner, `.mind-meld-log.md`, and `mm diff` annotations all reflect the split so cross-machine work is visible.

---

## Concurrency Safety

**Lockfile:** `~/.config/mind-meld/mind-meld.lock` — PID-based.

- Acquired at the start of `push`, `pull`, and `gc`.
- Contains the PID of the holding process.
- Stale locks (PID no longer running) are cleaned up automatically.
- If lock is held by a running process, fail with: "Another mm operation is running (PID {pid}). Wait for it to finish or remove ~/.config/mind-meld/mind-meld.lock."

**GC safety:** `mm gc` checks ALL device manifests before deleting any blob. A blob is only deleted if it is referenced by zero manifests. This is safe even if another device pushes concurrently — the new blob won't be in any manifest yet, but it also won't be in the delete set (it was just uploaded, not listed during the gc scan).

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
    "*.tmp",             # atomic-write leftovers from disk-full failures — don't propagate
    ".mind-meld-log.md",   # generated by pull, not synced back
]
```

### Sync Log (`.mind-meld-log.md`)

After `mm pull`, a `.mind-meld-log.md` file is written to each affected project directory. This gives Claude Code awareness of what changed from other machines:

```markdown
# Mind Meld Activity

Last pull: 2026-03-18 10:00 UTC from **MacBook Pro** (`abc123`)

## New from other machine
- memory/user_role.md

## Updated from other machine
- memory/feedback_testing.md
```

This file is excluded from sync (listed in EXCLUDED) so it doesn't propagate back. It's a local breadcrumb for Claude Code to discover cross-machine context.

---

## Multi-Source Sync

Mind Meld supports syncing multiple data sources beyond `~/.claude`. Each source is defined in `[[sync.sources]]` in config.toml.

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

`mm init` auto-detects known sources (e.g. if `~/.gstack` exists, it is added as a gstack source with default include patterns). Users can add or modify sources in config.toml.

### Source-Scoped Operations

- **Push** always pushes all sources. No `--source` flag on push.
- **Pull, status, diff** accept an optional `--source NAME` flag to operate on a single source for troubleshooting.
- **Deletions** during pull are scoped to the selected source.
- **Sync logs** are written only for the claude source. Generic sources don't produce sync logs.
- **GC** iterates `sources.*.files` in all manifests (with a backward-compat shim for v1 manifests).

---

## Error Handling

### Error Hierarchy (`mind-meld/errors.py`)

```python
class MindMeldError(Exception): ...           # base — all mm errors
class CryptoError(MindMeldError): ...         # encryption/decryption failures
class StorageError(MindMeldError): ...        # backend I/O failures
class ConfigError(MindMeldError): ...         # config parsing/validation
class ManifestError(MindMeldError): ...       # manifest corruption/incompatibility
class LockError(MindMeldError): ...           # concurrent operation conflict
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
mind-meld/
├── pyproject.toml
├── LICENSE                    # MIT
├── README.md                  # quickstart, install, usage examples
├── CONTRIBUTING.md            # how to contribute, dev setup, PR guidelines
├── CHANGELOG.md               # release notes
├── CLAUDE.md                  # instructions for Claude Code
├── SPEC.md                    # this file
├── docs/
│   ├── designs/
│   │   └── mind-meld-v1.md     # design decisions from spec review
│   ├── quickstart.md          # zero-to-syncing walkthrough
│   ├── encryption.md          # how encryption works, threat model, key management
│   └── troubleshooting.md     # common issues and fixes
├── src/
│   └── mind_meld/
│       ├── __init__.py        # __version__ via importlib.metadata (fallback "0.0.0+dev")
│       ├── cli.py             # typer app, command definitions, tri-state manifest fetch + recovery
│       ├── manifest.py        # directory walking, hashing, diffing, tombstone merge
│       ├── crypto.py          # AES-256-GCM encrypt/decrypt, Argon2id key derivation, gzip
│       ├── errors.py          # MindMeldError hierarchy
│       ├── sidecar.py         # local last-successful-push snapshot for corrupt-manifest recovery
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
    ├── test_merge.py          # JSONL union-merge, MEMORY.md line-merge
    ├── test_additive_sync.py  # additive-only pull, tombstones, conflict manifest union
    ├── test_conflict_copy.py  # Syncthing-style conflict-copy preservation on pull
    ├── test_recovery.py       # corrupt-manifest recovery chain (sidecar → peers → refuse)
    ├── test_version.py        # importlib.metadata version wiring, --version flag
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

**Exit criteria:** can `mm init` + `mm push` from one machine, see encrypted blobs in iCloud Drive.

### Phase 2 — Pull + Multi-Device + GC

9. `cli.py` — add `pull` (with atomic writes + deletion propagation), `devices`, `diff`
10. `cli.py` — add `gc` command (with manifest cross-check safety)
11. `synclog.py` — write `.mind-meld-log.md` per project after pull
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
22. `mm nuke --device DEVICE` to remove a device's data
23. CHANGELOG.md with initial release notes
24. PyPI package publishing via `pyproject.toml` (installable via `pip install mind-meld`)
25. GitHub repo setup: LICENSE, issue templates, CI (pytest + linting via GitHub Actions)

---

## Open Questions (Decide During Build)

1. ~~**Deletion propagation:**~~ **RESOLVED.** Truth-based manifests — push reflects current local state, deletions propagate automatically via push/pull/gc.
2. **Manifest conflict:** two devices push simultaneously. Last-write-wins is fine for v1 since manifests are per-device, but note this.
3. ~~**Large files:**~~ **RESOLVED.** 50MB default cap, configurable via `sync.max_file_size`.
4. ~~**Compression:**~~ **RESOLVED.** Gzip before encrypt. Session JSONs compress 5-10x.
5. ~~**PyPI name:**~~ **RESOLVED.** `mind-meld`.
6. ~~**CLI command name:**~~ **RESOLVED.** `mm`.
7. **GitHub org:** personal repo or create a dedicated org for discoverability?
