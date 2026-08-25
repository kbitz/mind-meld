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
8. **Host-interchangeable retro usage** — Claude session totals and Codex/Grok host snapshots appear as labeled families on one MODELS card. Usage consent is not file sync. See [Host Interchangeability](#host-interchangeability).

## Non-Goals

- Real-time sync or file watching (explicit push/pull only).
- Multi-user collaboration or sharing sessions between users.
- GUI or web interface.
- Cross-platform support (Mac-only — uses iCloud Drive).
- Syncing host session transcripts (Claude, Codex, OpenCode, or Grok).
- Walking a host home directory that mixes credentials and chat as a sync source.

---

## Documentation Philosophy

Every piece of documentation targets one of two audiences:

1. **End users** — people who want to sync their Claude Code sessions. The README and `docs/` folder should hold their hand from install through first successful sync, with copy-pasteable commands and no assumed knowledge beyond "I use Claude Code."
2. **Contributors** — developers who want to understand or extend the codebase. CONTRIBUTING.md covers dev setup, test running, and PR expectations. Code should be readable without comments where possible; comments explain *why*, not *what*.

**README structure:**
- One-liner description + badges (PyPI, license, CI)
- 30-second install (`pipx install git+https://github.com/kbitz/mind-meld.git@latest`). The `@latest` ref is load-bearing since v0.12.11 — it is a branch the release workflow force-advances each tag, so `pipx upgrade` keeps working. Never document the bare or `@vX.Y.Z` form as the primary install; a frozen tag pins the install forever and reports itself as up to date.
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

**This diagram is the core sync path only, not the full module list.** `src/mind_meld/` has grown well past it — notably the fleet-retro stack (`events.py`, `events_tail.py`, `token_usage.py`, `identity.py`), the conflict stack (`resolveflow.py`, `conflictdiff.py`, `conflictmtime.py`), and the shared leaves (`consoles.py`, `safety.py`, `fsutil.py`, `lockedjson.py`, `retention.py`, `skill_link.py`). **CLAUDE.md's Source Layout table is the authoritative one-line-per-module map** and is kept current per release; grep it for a filename before grepping the code.

Track 16A (v0.12.21) cut six modules out of `cli.py`. The load-bearing rule that came with them: `cli` imports those modules, and **none of them imports `cli`** — at module scope or function scope. `aggregator.py` reaches the CLI as a subprocess, never as an import. Enforced by `tests/test_module_boundaries.py` plus a CI grep gate, because ruff's F811 cannot see function-local shadowing.

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

[skills]                   # optional: per-machine retro-fleet link-maintenance policy
maintain_links = true      # false disables every agent link
                           # removing ONE link is not a config operation: delete it and mm leaves it deleted (v0.12.44)
# agents = ["claude", "codex"]  # when present, an exhaustive allowlist

[[sync.sources]]
name = "claude"
path = "~/.claude"
type = "claude"            # uses existing projects/*/memory|todos walker

[[sync.sources]]
name = "gstack"
path = "~/.gstack"
type = "generic"           # whitelist-based walker
include_dirs = ["projects", "analytics", "retros"]
include_files = ["retro-context.md", "greptile-history.md",
                 ".completeness-intro-seen", ".telemetry-prompted",
                 ".proactive-prompted", ".welcome-seen", ".codex-desc-healed"]
exclude_patterns = ["config.yaml", "projects/*/repo-mode.json",
                    "projects/*/land-deploy-confirmed"]

[crypto]
argon2_memory_kb = 65536   # Argon2id memory parameter in KB (default: 64MB). Lower for constrained environments.
```

**Backward compatibility:** Old configs using `sync.claude_dir` are auto-converted to a single claude source on load.

### Manifest Schema

The manifest is a **truth-based snapshot** of the local filesystem state. It always reflects the complete current state — files present locally are listed, files not present locally are omitted. Deletions propagate naturally: when a file is deleted locally, the next push produces a manifest without it.

**v2 manifests** carry a `sources` dict keyed by source name:

```json
{
  "device_id": "a1b2c3d4",
  "device_name": "MacBook Pro",
  "timestamp": "2026-04-08T12:00:00Z",
  "sources": {
    "claude": {
      "base_path": "/Users/alice/.claude",
      "files": {
        "projects/-Users-alice-myapp/memory/user_role.md": {
          "sha256": "e3b0c44298fc...",
          "size": 4096,
          "mtime": "2026-04-08T11:30:00Z"
        }
      }
    },
    "gstack": {
      "base_path": "/Users/alice/.gstack",
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
- v1 manifests (pre-v0.4 on-disk, top-level `files`, no `sources`) are auto-promoted to v2 shape by `normalize_manifest` on load: the files get wrapped as a `claude` source entry.
- Pre-Track-1B v2 manifests emitted a redundant top-level `files` mirror of the claude source. `normalize_manifest` now strips that mirror unconditionally (both v1 promotion and v2 passthrough) so the single source of truth is `sources[<name>]["files"]`. Every manifest loaded from disk goes through `load_manifest`, which enforces this — callers may rely on `manifest["sources"]` being the only place file data lives.

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
           [--conflict-mode prompt|keep-both|fail]      # conflict handling mode (default keep-both)
mm status [--source NAME]   # show local vs remote state, pending changes
                            # plus a line for broken retro-fleet links (absent and removed-by-user are NOT broken)
mm diag [--json]            # non-secret crypto / sync / breadcrumb triage dump; runs without a passphrase or a valid config
                            # top-level keys: mm_version, config, crypto_init, root_salt_drift, sidecar, storage_inventory, last_autorun, skill_links, host_skill_discovery
                            # `skill_links` rows: agent, target, store, store_state, store_version, status, maintain_links, readlink|detail
                            # status is one of ok | absent | removed-by-user | live-checkout | foreign | foreign-dangling | dangling-ours | dangling-ours-legacy | error
                            # removed-by-user = mm resolved that target before and the link is now gone (a deliberate deletion); absent = mm never installed there. Neither is broken.
                            # maintain_links is enabled | disabled (...) | unknown (config invalid: ...) | unknown (policy not resolved)
                            # `host_skill_discovery` is a sibling key (Grok inspect probe), never a skill_links row
                            # host_skill_discovery: host, status (ok | binary-absent | timeout | nonzero-exit | malformed-json | unsupported-schema), claude_skills_compat, retro_fleet_resolved, retro_fleet_path, grok_version
mm devices [--format table|json]   # list registered devices (json: stable schema for scripts / retro-fleet)
mm diff [--from DEVICE] [--source NAME]   # show what would change (dry run)
                                             # annotates modified files as write / merge / skip / conflict
mm gc [--dry-run] [--conflicts]
                            # delete orphaned blobs and local retention data; --dry-run previews candidates without mutation
                            # with --conflicts, also reap .sync-conflict-* files >30d
mm sources                  # list configured sync sources with status
mm conflicts                # list unresolved .sync-conflict-* files across sources
mm resolve [PATH]           # interactively resolve conflict files (unified diff + pick winner)
mm autopull                 # silent pull for Claude Code (one-line output, never prompts)
mm autopush                 # silent push for Claude Code (one-line output, never prompts)
mm enable-source NAME       # turn a configured sync source ON for this machine
                            # NAME=grok adds its scoped skills/ commands/ rules/
                            # source and keeps [retro].grok_host_usage enabled
mm disable-source NAME [--force]   # turn a configured sync source OFF for this machine; --force accepts unknown names (forward-compat for not-yet-shipped sources)
                            # NAME=grok disables that scoped source and clears
                            # its retained usage-consent compatibility bit
mm reconfigure-sources      # re-run the source picker against current config + new defaults
mm migrate-config [--yes] [--dry-run]   # idempotent: append missing recommended exclude_patterns to existing [[sync.sources]] entries; preserves user customizations
mm refresh-identity [--json]   # force-refresh the local identity (author-email) cache feeding mm-push event rows; --json emits the resolved set
mm install-skills [--agent KEY]  # check/install the retro-fleet skill link for every authorized agent; reports declined rows, repairs mm's own dangling and legacy links, never overwrites a foreign one
                               # --agent is repeatable: persist a maintenance grant without enabling sync or usage reading, then install every authorized agent
                               # also the documented undo for a deleted link: `mm push` leaves an absent link deleted (v0.12.44), `mm install-skills` / `mm init` put it back
mm log [--source NAME] [--since DATE] [--action ACTION] [--verb VERB] [--limit N] [--format jsonl|table]
                            # query the per-file pull/push history log
mm retro-fleet [WINDOW] [--no-author-filter]
                            # render fleet retrospective markdown to stdout (default 7d). Public CLI surface for the /retro-fleet Claude Code skill; safe to invoke directly for scripted exports.
                            # Skill-internal second-pass flags (v0.12.0+): --theme TEXT (≤3x), --noteworthy TEXT, --name TEXT — supplied by the /retro-fleet skill on its second pass to render the pixel-aligned ASCII card up top. Direct CLI users typically don't pass these. --no-save is a hidden no-op as of v0.12.39.
```

### Global Flags

```
--verbose                      # show each file hashed, each blob transferred, timing info
--dry-run                      # show what would happen without doing it
--install-completion           # install shell completion for mm (typer built-in; edits your shell startup file)
--show-completion              # print the completion script instead of installing it
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
11. For conflict-copy decisions, rename the local file to `<stem>.sync-conflict-<ts>-<device>.<ext>` before writing remote to the canonical path. With `--conflict-mode prompt`, prompt per-file instead. With `--conflict-mode fail`, preflight all files and exit **3** with the predicted-conflict list if any file would conflict — no writes happen. (Exit 3, not 2 — see Conflict mode below for why the distinction from typer's usage-error exit is load-bearing.)
12. Pull is **additive-only:** local files absent from the remote manifest are kept. Deletions propagate only via tombstones produced by a subsequent push from the originating device.
13. Write `.mind-meld-log.md` per affected project (claude source only), including `## Conflicts` and `## Skipped (local was newer)` sections when relevant.
14. Release lockfile.
15. Print summary with split counts: written / merged / skipped / conflicted / failed.

### `mm gc`

1. Acquire lockfile.
2. Sweep this device's stale `tmp*.tmp` files left behind by a crashed push (runs under the lock already held, so no concurrent writer can race it).
3. Download and decrypt ALL device manifests.
4. Collect the set of all SHA-256 hashes referenced by any manifest.
5. List all blobs in `data/` across all devices.
6. Delete blobs not in the referenced set.
7. Reap `mm-events` files older than `EVENTS_RETENTION_DAYS` (90). Always-on fleet policy, not opt-in.
8. Reap `session-tokens.json` cache entries whose underlying jsonl is gone, or whose most recent `by_day` key is more than 90 days old.
9. With `--conflicts`, also reap `.sync-conflict-*` files older than `CONFLICT_AGE_DAYS` (30).
10. Supports `--dry-run`: each executed retention reaper selects the same candidates as apply mode, prints a stable summary (including zero counts), and makes no deletion or cache write. Token-cache preview holds a shared read-only lock, so inspecting a missing or malformed cache never creates, rewrites, re-permissions, or normalizes it. Apply mode counts a deletion only after it succeeds and reports failed or skipped work separately; use `mm gc -v` for safe per-path detail. `--conflicts` opts into the conflict-sidecar reaper in both modes.
11. Release lockfile.
12. Print summary (blobs deleted, bytes freed).

The reapers and the tmp sweep live in `retention.py`; the `@app.command()` shell stays in `cli.py`.

### `mm status [--source NAME]`

1. Build local manifest (no upload).
2. Fetch remote manifest for current device.
3. Fetch device list.
4. Print: local file count, remote file count per device, pending pushes, pending pulls.
5. If `--source` is given, show changes for that source only. Otherwise group changes by source.
6. Print the last `autopull` / `autopush` breadcrumb (timestamp + outcome + `detail`), surfacing the `no-sources` and `degraded` outcomes the auto commands write.
7. **Staleness marker (v0.12.21).** A breadcrumb older than 48h renders as `stale — no autorun in Nh`. The breadcrumb is written from *inside* the command, so any failure before typer's runner — a module-scope `ImportError` being the obvious one — writes nothing at all and step 6 would otherwise report the last `success` forever. This is the one degradation neither the `no-sources` nor the `degraded` breadcrumb can cover, because both are written by code that never ran. Best-effort: a missing or unparseable timestamp yields no marker rather than an error.

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

**Last-resort escape hatch:** `mm recover --abandon-manifest` exists for the corrupt + no-sidecar + no-peer scenario where step 3 refuses. The subcommand is **destructive by design** — it quarantines the corrupt manifest to `<key>.corrupt-<ts>` (crash-durable via atomic-write + fsync + unlink, not plain rename) and allows the next push to proceed with `remote_manifest=None`. The accepted cost: any files deleted locally since the last successful push lose their deletion records, so peers will see those files come back on their next pull. Typed-`RESET` confirmation gates the operation. Never call this path without surfacing the deletion-history loss to the user first.

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
4. **Conflict-copy (C, INVERTED in v0.9.2):** local has diverged AND isn't mergeable AND isn't newer — keep local at canonical, write the REMOTE bytes to `<stem>.sync-conflict-<YYYYMMDD-HHMMSS>-<device>.<ext>` (Syncthing convention, collision suffix on clash). Local edits stay at canonical; remote bytes are preserved alongside.
5. **Update-base (U):** remote hash matches local hash — no I/O, just refresh the last-synced state.

Pull re-reads local hash and mtime at apply time so the decision reflects the actual state when writing, race-safe against editors running during a long pull.

**Inversion (v0.9.2 BREAKING).** Pre-v0.9.2 did the opposite for [C]: local was renamed out to the sidecar and remote bytes were written to canonical. The inversion makes the visible `.sync-conflict-*` file hold the *surprising* version (remote, from elsewhere) rather than the working version (local, on this machine). Sidecar-write failure is per-file isolated and does NOT need a rollback dance — local is never overwritten in the conflict path.

**Pre-inversion file migration.** Files produced by pre-v0.9.2 code carry no marker. The first lock-protected discovery in `mm pull` or `mm resolve` migrates them by renaming to `<stem>.sync-conflict-v0-<ts>-<dev>.<ext>` — the `v0-` prefix is reserved for migrated files; post-v0.9.2 code never produces it directly. The dual-mode resolve dispatch is keyed by the prefix, NOT by timestamp (sound — post-inversion code is the only producer of un-prefixed files). `mm conflicts` is intentionally read-only and does NOT migrate (it's lockless and would race autopull, codex-2 #5).

**Strict pull-start fleet-version refusal (load-bearing).** `mm pull` exits non-zero before any I/O if any peer's `last_seen_version < 0.9.2`, OR if any peer's `device.json` is corrupt (can't read its version). Per-peer classification: safe / inactive (registered, never pushed → ALLOW) / pre-v0.9.2 (REFUSE) / dropped (REFUSE by storage key, codex-2 #3). The refusal cites every offending peer; recovery is to upgrade peers and have each push once. Last-resort: hand-edit `device.json` to add `"last_seen_version": "0.9.2"` only after verifying the peer is actually upgraded. Without this gate, a pre-v0.9.2 peer pushing now would produce conflict files under the OLD direction (canonical = remote, sidecar = local), and a v0.9.2 puller would silently mis-resolve them under the NEW dual-mode dispatch.

**Conflict mode.** `mm pull --conflict-mode` takes one of three values:
- `keep-both` (default): auto-keep-both via the inverted [C] path — local stays at canonical, remote lands in `.sync-conflict-*`.
- `prompt`: per-file prompt (unified diff + pick `(m)erge` / `(l)ocal` / `(r)emote` / `(s)kip` / `(a)bort`, default skip in v0.11.1+; pre-1.0 letters `b` / `both` accepted as deprecated alias mapping to skip). Since v0.12.10 it also renders each side's timestamps and a recency verdict, but display-only — there is deliberately no `(n)ewer` shortcut at this site, because `_apply_incoming_file` already skipped before prompting whenever local was the newer file, so `(n)` would be a redundant alias of `(r)`.
- `fail`: preflight every file via `_predict_pull_outcome`. If any file would conflict, print the list and exit **3** with **no writes**. For CI use. Best-effort — a file edited between preflight and apply may still produce a `.sync-conflict-*` (TOCTOU); re-run pull to surface it. Exit 3 (not 2) distinguishes "conflict refusal" from typer/click's usage-error exit 2, so a stale script using the removed `--no-prompt` / `--resolve-interactive` flags can't be silently misclassified.

**Conflict-prompt UX (v0.11.1, extended v0.12.8 / v0.12.10).** Both prompt sites (inline `mm pull --conflict-mode prompt` and `mm resolve`) render:
1. Color LOCAL/REMOTE banners above the diff (red + green gutters, peer-name attribution on the REMOTE banner via `lookup_device_by_short_id`).
2. Created/modified timestamps per side plus a `-> SIDE is newer by N` recency verdict (v0.12.10). The remote side's "created" reads `pulled` — it is the local sync time, not the peer's real creation, because the manifest carries only modified time.
3. Three-number divergence summary `M removed-or-replaced / N added-or-replaced / K total diff lines`.
4. The unified diff (`+`/`-` colored as before).
5. Concrete-action option copy: `(l)ocal → discard <conflict>, keep <canonical>` / `(r)emote → overwrite <canonical> with <conflict> bytes` / `(s)kip → leave both files; run `mm resolve` later or delete manually` / `(a)bort → stop reviewing; exit`.

Default flipped from `b` to `s` in v0.11.1; `b` / `both` aliased with one-time stderr notice until 1.0. The prior `c` / `f` letters from pre-v0.9.0 remain loud-rejected (real silent-data-loss risk pre-inversion). Since v0.12.8 the default key is **always** `(s)kip` at both sites — Enter never auto-accepts a merge, and since v0.12.10 it never auto-accepts a recency guess either.

The shared leaf renderers (`render_prompt`, `render_banner`, `render_capped_diff`, `count_divergent_lines`, `format_ts`, `format_age_delta`, `newer_side`, `render_time_line`, `render_verdict`) live in `conflictdiff.py`; site-level dispatch over the four canonical-exists × inversion-mode shapes stays at each call site.

**Post-hoc resolution commands:**

- `mm conflicts` — list every `.sync-conflict-*` file across synced sources with age, canonical sibling, and per-row Mode column (`pre-v0.9.2` for `v0-`-prefixed files, `v0.9.2+` otherwise). Read-only; does NOT migrate pre-inversion files.
- `mm resolve [PATH]` — walk conflicts (or a single path) interactively. Shows banners + timestamps + divergence summary + unified diff, then prompts `(m)erge` / `(l)ocal` / `(r)emote` / `(n)ewer` / `(p)romote` / `(s)kip` / `(a)bort`. `(p)romote` keeps BOTH by giving the conflict file its own first-class filename; `(n)ewer` (v0.12.10) remaps onto the existing `(l)` / `(r)` dispatch rather than adding an apply branch, is offered only when both sides' mtimes are readable, and re-prompts on an exact tie instead of guessing. Dual-mode dispatch by filename prefix: `v0-` → pre-inversion ops ((l) renames sidecar over canonical, (r) unlinks sidecar); no prefix → post-inversion ops ((l) unlinks sidecar, (r) renames sidecar over canonical). Acquires the mm lockfile so autopull can't race the rename/unlink. Migrates pre-inversion files to `v0-` prefix on first discovery. Exits 1 if any per-conflict rename/unlink/read fails, so scripts can detect partial failure; the walk still continues through every conflict. Lives in `resolveflow.py` (the `@app.command()` shell stays in `cli.py`).
- `mm gc --conflicts` — reap `.sync-conflict-*` files older than `CONFLICT_AGE_DAYS` (30 days). Matches both prefixed and un-prefixed forms via `is_conflict_filename`.

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

**`generic` type** — whitelist-based walker. Walks only the directories listed in `include_dirs` (recursively) and includes individual files listed in `include_files` at the source root. Nothing else is synced. This is used for `~/.gstack` and for the Codex / OpenCode customization allowlists.

**`grok` type** — Claude-shaped walker for `~/.grok`. Hardcodes `skills/`, `commands/`, and `rules/` at the source root. Sessions, credentials, and `config.toml` are never entered. See [Host Interchangeability](#host-interchangeability).

### Excluded Patterns

Hardcoded global list (universal junk; not configurable):

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

### Per-source `exclude_patterns` (load-bearing)

Each `[[sync.sources]]` entry may carry an `exclude_patterns: list[str]`
of fnmatch globs evaluated against the relative path inside the source.
Extends the global EXCLUDED list; per-source globs are scope-prefixed by
spelling them out (e.g. `projects/*/repo-mode.json`, `**/cache.json`).
Validated as `list[str]` of strings at config load via
`_validate_exclude_patterns`; malformed shapes raise `ConfigError` at the
load boundary, not mid-sync.

The default `gstack` source ships with
`["projects/*/repo-mode.json", "projects/*/land-deploy-confirmed"]` —
gstack's per-machine artifacts (7-day TTL solo-vs-collaborative cache
and deploy-config-hash markers, both recomputed locally per device).
Without these, every pull conflict-copies them on every device every day
(2026-04-24 first-pull regression).

**Consumer-boundary filter (load-bearing).** The exclude filter applies at
TWO call sites — both **after** `_fetch_remote_manifest` returns:

1. `_pull_core`: filters peer manifests in `manifest_cache` after
   `_prefetch_manifests` returns and BEFORE `collect_tombstones` and the
   per-source download loop.
2. `_push_core`: filters the manifest returned by `_recover_prior_manifest`
   (covers `ok` / `sidecar` / `peer-fallback` branches uniformly) BEFORE
   `generate_tombstones`.

The filter MUST NOT be applied inside `_fetch_remote_manifest`. `mm gc`
walks raw manifests via `_fetch_remote_manifest` to compute referenced
blobs; a filtered manifest there would mark live peer blobs as orphans
and silently delete them. Pinned by
`test_mm_gc_does_not_orphan_excluded_path_blobs`.

**Tombstone-suppression invariant.** Adding a path to `exclude_patterns`
must NOT generate a deletion tombstone on the next push. The walker drops
the path from the local manifest, AND the consumer-boundary filter strips
it from the prior remote manifest before `generate_tombstones` compares,
so the path is invisible to the carry-forward + new-tombstone logic.
Removing a glob brings the path back as new (no spurious tombstone). The
sidecar recovery branch passes through the same filter so a corrupt-
manifest recovery on a freshly-migrated config doesn't re-introduce
pre-exclude paths via the sidecar (codex-2 #2).

**Migration UX (visible-failure contract).** Existing configs need to opt
in by running `mm migrate-config` (idempotent, prompts before mutating).
Interactive `mm pull` / `mm push` prompt-once if recommended excludes are
missing. autopull / autopush NEVER auto-mutate config — they record the
missing-excludes signal to `~/.config/mind-meld/migration-state.json` and
let `mm status` surface it. Silent config mutation in a hook would be
exactly the class of "wedged sync I never noticed" failure the visible-
failure contract exists to prevent.

### Pull / push history log

`mm log` queries `~/.config/mind-meld/pull-history.jsonl` (mode 0600,
fcntl.flock-guarded appends, 1MB cap with line-boundary rotation to
`.1`). Records every per-file pull/push action: `written` / `merged` /
`skipped` / `conflicted` / `excluded` / `uploaded` / `failed`. Forensic
audit trail; `mm log` failures (disk full, perms flip) NEVER block a
calling pull/push.

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

- **`claude`** — The original walker. Scans `projects/*/memory/` and `projects/*/todos/`. One claude source is always present. Session jsonls under `projects/` are never synced.
- **`grok`** — Claude-shaped walker. Scans `skills/`, `commands/`, and `rules/` at `~/.grok`. Sessions and `auth.json` are never synced.
- **`generic`** — Whitelist-based walker. Walks only `include_dirs` recursively and picks up `include_files` at the source root. Used for `~/.gstack`, `mm-events`, and the Codex / OpenCode customization allowlists.

Default generic sources (see `config.py:DEFAULT_SOURCES`):

- **`gstack` / `gstack-extend` / `mm-events`** — structured activity and the fleet event log.
- **`codex`** — `skills/`, `plugins/`, `AGENTS.md`. Not `config.toml` (credentials). Not `sessions/`.
- **`opencode`** — agents / commands / modes / plugins / skills / tools / `AGENTS.md`. Whole-file config stays local.

**`grok` is a source type** (`type: "grok"`). The walker hardcodes `skills/`, `commands/`, and `rules/` at the source root — the same shape as Claude's hardcoded `memory/` + `todos/`. Sessions, `auth.json`, and `config.toml` are never walked. `mm enable-source grok` is the one verb: it appends the source and keeps the usage-consent bit so token totals still publish. See [Host Interchangeability](#host-interchangeability).

### JSONL Merge on Pull

`.jsonl` files (common in gstack for review logs, analytics, etc.) use a merge strategy instead of overwrite on pull:

1. Union of lines from local and remote (byte-exact dedup after whitespace strip).
2. Sort by `ts` field if present in JSON lines.
3. Lexicographic tiebreaker for non-JSON or timestamp-less lines.

The merged result becomes the new local truth and propagates on the next push.

### Backward Compatibility

v2 manifests carry a `sources` dict keyed by source name. Compat with older on-disk manifests is handled at the read boundary, not by mirroring keys in the write format:

- **v1 manifests on disk** (pre-v0.4 shape: top-level `files`, no `sources`) are auto-promoted by `normalize_manifest`: the files become a `claude` source entry under `sources`, and the top-level `files` key is scrubbed post-copy so downstream consumers see a single-source-of-truth shape.
- **Pre-Track-1B v2 manifests** (v2 shape but with a redundant top-level `files` mirror) have that mirror scrubbed on load for the same reason. No data is lost: the payload already lives under `sources.claude.files`.
- **Old configs** using `sync.claude_dir` are auto-converted to a single claude source on load.

### Auto-Detection

`mm init` auto-detects known sources (e.g. if `~/.gstack` exists, it is added as a gstack source with default include patterns). Users can add or modify sources in config.toml.

### Source-Scoped Operations

- **Push** always pushes all sources. No `--source` flag on push.
- **Pull, status, diff** accept an optional `--source NAME` flag to operate on a single source for troubleshooting.
- **Deletions** during pull are scoped to the selected source.
- **Sync logs** are written only for the claude source. Generic sources don't produce sync logs.
- **GC** iterates `sources.*.files` in all manifests (with a backward-compat shim for v1 manifests).

---

## Host Interchangeability

Claude, Codex, OpenCode, and Grok are peers for **fleet usage display**, not for **whole-home-directory sync**. Grok also has a narrow customization source. The design and the remaining work live in `docs/designs/host-parity.md`. This section is the product contract.

### What "parity" means

| If someone asks for… | The answer |
|---|---|
| Grok (or Codex) rows next to Claude on the MODELS card | Tracks 22A / 23A. 18D is the Grok reader; 21A is consent + publish. |
| Grok skills / home rules roaming across Macs | `type: "grok"` source. Walker hardcodes `skills/`, `commands/`, `rules/`. Same verb as usage: `mm enable-source grok`. |
| `retro-fleet` installed into `~/.grok/skills` | No. Plan C resolved in v0.12.43: Grok 1.0.5 already discovers `~/.claude/skills` via default-on Claude compat, so mm maintains no Grok skill link. `mm diag --json` reports that under the sibling `host_skill_discovery` key, never as a `skill_links` row. |
| Uploading Grok / Codex / Claude sessions | No. Claude does not sync `~/.claude/projects/**/*.jsonl` either. |

### Why we do not sync the Grok home root

Grok's installed root mixes `auth.json`, `config.toml`, session streams (`updates.jsonl` is not a metadata-only ledger), chat history, plans, tool output, logs, and worktrees. Walking `~/.grok` as `include_dirs: ["."]` would upload prompts. The `type: "grok"` walker never enters those trees.

`mm enable-source grok` is now both: it appends the scoped `type: "grok"` source and authorizes the 18D reader. The walker never opens `sessions/` or `auth.json`. A root-level `include_dirs: ["."]` walk is not possible because those keys are not on the Claude-shaped row.

### What the events tail still does not walk

Claude's tail also emits a `sessions-snapshot` (repos, session counts, skill names). Codex and Grok do not. Their session files are not a metadata-only project ledger, and an encoded cwd on disk is a path, not something that goes on the wire. That gap is documented, not a missed 18D task. Do not close it by decoding host paths.

### Planned follow-ups (not 18D / 21A)

1. **Plan A (scheduled):** Groups 22 and 23 render accepted host snapshots beside Claude totals, with coverage, not false zeros.
2. **Plan B (Track 22B):** a `type: "grok"` source named `grok`. Walker hardcodes `skills/`, `commands/`, `rules/`. Never `sessions/`, credentials, or `config.toml`.
3. **Plan C (resolved, v0.12.43):** mm maintains **no** Grok skill link. Grok 1.0.5 discovers `~/.claude/skills` at the same documented priority tier as `~/.grok/skills` (verified with `grok inspect --json`), so a fourth `skill_link` target would duplicate a link the host already reads. `mm diag` reports discovery under `host_skill_discovery` instead. Exit criterion for any future host: mm maintains a skill link only for hosts that do not discover `~/.claude/skills`. See `docs/designs/host-parity.md`.

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

> **Historical: this is the original v1 build plan, not the current tree.** It is kept alongside the Implementation Order below as a record of what was scoped up front. Several entries were never built (`CONTRIBUTING.md`, `docs/quickstart.md`, `docs/encryption.md`, `docs/troubleshooting.md`), and `src/mind_meld/` has roughly tripled since — Track 16A (v0.12.21) alone added `consoles.py`, `conflictmtime.py`, `skill_link.py`, `events_tail.py`, `resolveflow.py`, and `retention.py`. For the current tree, read **CLAUDE.md's Source Layout table** (one line per module, kept current per release) and `docs/invariants/` for the per-topic load-bearing rules.

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
    ├── test_recovery.py       # corrupt-manifest recovery chain (sidecar → peers → refuse) + abandon-manifest destructive path
    ├── test_recover.py        # mm recover --abandon-manifest unit tests (flag, typed RESET, quarantine durability, collision)
    ├── test_diag.py           # mm diag secrets-boundary + degraded scenarios (missing/corrupt crypto_init, no config)
    ├── test_version.py        # importlib.metadata version wiring, --version flag
    └── test_integration.py    # full push→pull round-trip, deletion propagation, GC safety, init two-tier guard
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
