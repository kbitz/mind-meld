# CLAUDE.md

## Project
claude-session-sync (css) — CLI tool for syncing ~/.claude session data across machines via pluggable storage backends.

## Stack
Python 3.11+, typer, cryptography, keyring, rich. boto3 optional (S3 backend only).

## Key Principles
- No API server. CLI talks directly to the storage backend.
- Two storage backends: S3-compatible (boto3) and local folder (Dropbox/GDrive/iCloud via pathlib).
- Both backends implement the same `StorageBackend` ABC — CLI logic is backend-agnostic.
- **End-to-end encrypted.** All data (sessions, manifests, artifacts) encrypted client-side with AES-256-GCM before touching any storage backend. The storage layer never sees plaintext. This is a hard invariant — no code path may write unencrypted session data to storage.
- Manifest-based diffing: SHA-256 hash every file, only upload/download changes.
- Content-addressed storage: blobs stored by hash, not by path.

## Source Layout
src/css/{cli,manifest,crypto,devices,paths,config}.py
src/css/storage/{base,s3,local}.py

## Testing
pytest. Use moto for S3 mocking, tmp_path for local backend. Run: `pytest tests/`

## Commands
css init | push | pull | status | devices | diff | verify | rekey

## Spec
See SPEC.md for full architecture and data model.
