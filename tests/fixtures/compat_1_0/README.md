# Frozen Mind Meld 1.0 reader fixtures

Generated once from the 1.0.0 implementation on 2026-09-22, using isolated
temporary storage and the shipped constructors. These payloads must remain
readable throughout 1.x. Do not regenerate them to make a failing reader pass.

- `mm-crypto-init`: `bootstrap_crypto_init`, Argon2 memory **1,024 KB**.
- `blob.enc`: `encrypt(b"Mind Meld 1.0 compatibility fixture.\n", ...)`.
- `manifest.json`: `build_manifest_v2` / `serialize_manifest`, with its source
  base path normalized to `/compat/notes` and a representative tombstone added.
- `mm-push.json`: `make_mm_push_event`, version 1.0.0, timestamp 2026-09-22 noon UTC.
- `host-usage-snapshot.json`: `make_host_usage_snapshot`, with nonempty family
  and per-model counters for that UTC day.
- `device.json`: `register_device` followed by `update_last_seen` at 1.0.0.

Public test passphrase: `compat-1.0-public-test-passphrase`. This is fixture
material only, never a credential or an example production Argon2 cost.
Random crypto salts/nonces and creation timestamps are frozen in the bytes.
Tests consume these payloads; they do not round-trip through today's writers.

The separately maintained `../cli_surface_1_x.json` golden describes the
current 1.x CLI and may be extended after classifying a compatible MINOR.
