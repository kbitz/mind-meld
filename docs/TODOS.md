# TODOS

Inbox for unprocessed items. Other skills (`/full-review`, `/investigate`,
`/pair-review`, manual) append here; `/roadmap` drains and organizes into
`docs/ROADMAP.md`.

## Unprocessed

- **[review] `_save_and_register` needs register-failure rollback** — `cli.py:_save_and_register` persists config → registers device → stores passphrase. If `register_device` raises (iCloud hiccup, transient StorageError) after `save_config` succeeded, the local config now claims a `device_id` that `devices/` on storage doesn't contain. Peers scanning `devices/` via `_select_devices` never discover this device — push writes manifests under an ID no one is listening for. Pre-existing behavior (present before Track 2A; Codex adversarial review 2026-04-24 flagged during /review). Fix options: (a) swap order to register BEFORE save_config, (b) on register failure, delete the saved config, (c) add a lazy self-repair path in push that calls register_device if the device file is missing. Option (a) is simplest but config-without-device is its own ugly state; option (c) is most robust. _src/mind_meld/cli.py, src/mind_meld/devices.py, ~40 lines._ (S)
- **[review] Cross-device source rename drift partitions sync** — Track 2A's type-keyed sync-log fix addresses SAME-device renames. Cross-device, manifests are still keyed by `src_name` (`manifest.py`, `_pull_core`'s `local_sources_map[src_name]` lookup), so if device A renames "claude" → "work-claude" but device B keeps "claude", B's pull skips A's manifest via the unknown-source warning path. Codex adversarial review 2026-04-24. Fix: cross-device source identity needs to key off `(type, signature)` or similar, not raw name. Bigger design change — likely a follow-up track or a SPEC.md-documented known limitation for v1.0. _src/mind_meld/cli.py, src/mind_meld/manifest.py, SPEC.md, ~100 lines._ (M-L)
