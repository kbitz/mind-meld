# CLAUDE.md

## Project
MemSync (msync) — CLI tool for syncing ~/.claude session data across machines via pluggable storage backends.

## Stack
Python 3.11+, typer, cryptography, argon2-cffi, keyring, rich. boto3 optional (S3 backend only).

## Key Principles
- No API server. CLI talks directly to the storage backend.
- Two storage backends: S3-compatible (boto3) and local folder (Dropbox/GDrive/iCloud via pathlib).
- Both backends implement the same `StorageBackend` ABC — CLI logic is backend-agnostic.
- **End-to-end encrypted.** All data (sessions, manifests, artifacts) encrypted client-side with AES-256-GCM before touching any storage backend. The storage layer never sees plaintext. This is a hard invariant — no code path may write unencrypted session data to storage.
- **Scoped sync.** Only syncs `memory/` and `todos/` within each project — not sessions, settings, or other git-tracked files.
- **Truth-based manifests.** Manifests are complete snapshots of local state. Deletions propagate automatically — no separate prune step.
- **Sync log.** After pull, writes `.memsync-log.md` per project so Claude Code knows what changed from other machines.
- Manifest-based diffing: SHA-256 hash every file, only upload/download changes.
- Content-addressed storage: blobs stored by hash, not by path.
- Gzip compression before encryption. Versioned blob format (v0x01).

## Source Layout
src/memsync/{cli,manifest,crypto,errors,devices,paths,config,lockfile,synclog}.py
src/memsync/storage/{base,s3,local}.py

## Testing
pytest. Use moto for S3 mocking, tmp_path for local backend. Run: `pytest tests/`

## Commands
msync init | push | pull | status | devices | diff | gc | verify | rekey

## Spec
See SPEC.md for full architecture and data model.
See docs/designs/memsync-v1.md for design decisions from spec review.
