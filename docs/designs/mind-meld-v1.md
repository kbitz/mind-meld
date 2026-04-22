# Mind Meld v1 Design Decisions

> Generated from CEO plan review on 2026-03-18.
> Supplements SPEC.md with scope expansions and architectural decisions made during review.

## Rebrand

The project is renamed from `claude-session-sync` / `css` to **Mind Meld** / `mm`.

| Old | New |
|-----|-----|
| `claude-session-sync` | `mind-meld` (PyPI package) |
| `css` | `mm` (CLI command) |
| `src/claude_session_sync/` | `src/mind_meld/` |
| `~/.config/claude-session-sync/` | `~/.config/mind-meld/` |

## Accepted Scope Additions (beyond original SPEC.md)

### 1. Compression (gzip before encrypt)
Session JSONs compress 5-10x. Gzip at default level before encrypting, decompress after decrypting. Cheaper to design in now than retrofit later (would require format migration).

### 2. File format versioning byte
Encrypted blob format becomes: `[version:1][salt:16][nonce:12][compressed_ciphertext+tag]`. Version `0x01` = initial format. Enables graceful format evolution without breaking migrations.

### 3. Large file cap
Default 50MB per file, configurable via `sync.max_file_size` in config.toml. Files exceeding the cap are skipped with a warning. Prevents a single large artifact from making push/pull unusable.

### 4. Garbage collection (`mm gc`)
Compares all device manifests against all stored blobs. Deletes blobs not referenced by ANY manifest. Supports `--dry-run`. Must hold a lockfile during execution to prevent races with concurrent pushes.

### 5. Truth-based manifests (deletion propagation)
Push always reflects the complete current state of `~/.claude/projects/`. If a file was deleted locally, the new manifest simply omits it. Pull applies the full manifest state, including deletions. GC cleans up orphaned blobs. No separate `prune` command needed.

### 6. MINDMELD_PASSPHRASE env var fallback
When no OS keyring is available (headless, SSH sessions, CI), fall back to the `MINDMELD_PASSPHRASE` environment variable. Keyring remains the default and preferred method.

### 7. argon2-cffi dependency
The `cryptography` library does not include Argon2id. `argon2-cffi` is added as an explicit core dependency for key derivation.

### 8. Conflict resolution (iCloud + Dropbox)
- If manifest decryption fails, treat as empty and do a full re-push.
- Detect iCloud-style conflicted copies (`filename 2.ext`) and Dropbox-style copies (`filename (conflicted copy YYYY-MM-DD).ext`), try decrypting all, keep the one with the newer manifest timestamp, delete the losers.

### 9. PID-based lockfile
`~/.config/mind-meld/mind-meld.lock` prevents concurrent same-device operations (double push, gc during push). Stale locks (PID no longer running) are cleaned up automatically.

### 10. Atomic writes on pull
All file writes during pull use write-to-tmp-then-rename pattern (`os.rename` is atomic on POSIX). Prevents half-written files on crash or ctrl-C.

### 11. Structured error hierarchy
`mind-meld/errors.py` defines:
- `Mind MeldError` (base)
- `CryptoError` (encryption/decryption failures)
- `StorageError` (backend I/O failures)
- `ConfigError` (config parsing/validation)
- `ManifestError` (manifest corruption/incompatibility)
- `LockError` (concurrent operation conflict)

## Updated Dependency List

**Core (always installed):**
```
typer >= 0.9
cryptography >= 42.0
argon2-cffi >= 23.1
keyring >= 25.0
rich >= 13.0
```

Install: `pipx install mind-meld`

## Updated Encrypted Blob Format

```
[version:1][salt:16][nonce:12][gzip_compressed_ciphertext + GCM_auth_tag]
```

- Byte 0: format version (`0x01`)
- Bytes 1-16: random salt (unique per encryption)
- Bytes 17-28: random nonce (unique per encryption)
- Bytes 29+: AES-256-GCM ciphertext of gzip-compressed plaintext, with 16-byte auth tag appended

## Deferred (docs/TODOS.md)

1. **Selective sync** — `sync.include` / `sync.exclude` in config.toml to filter which projects are synced. Deferred because syncing everything is acceptable for v1.

## Architecture Notes

### Error handling pattern
All errors surfaced to users follow the format: `[operation] [what failed] [why] [what to do]`. Example: `pull: failed to decrypt blob abc123.enc: GCM tag mismatch — wrong passphrase or corrupt data. Re-push from source device.`

### Observability
- `--verbose` flag: show each file hashed, each blob uploaded/downloaded, timing
- Push/pull summary: files scanned, changed, bytes transferred, elapsed time

### Open questions resolved
1. **Deletion propagation:** Yes — truth-based manifests. Push reflects reality.
2. **Large files:** 50MB cap, configurable.
3. **Compression:** Yes — gzip before encrypt.
4. **CLI name:** `mm` (project = Mind Meld).
