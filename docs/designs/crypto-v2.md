# Crypto v2: process-scoped master key + HKDF

**Status:** Implemented in v0.6.0.
**Predecessor:** `crypto-v1` (shipped 0.1.0 through 0.5.1).
**Reviews:** `/plan-ceo-review` (2026-04-22) + `/plan-eng-review` (2026-04-22),
both with Codex outside-voice passes.

## Problem

v1 derived an AES-256 key via Argon2id (3 iterations, 64MB memory, 1
parallelism) **once per file** using a random 16-byte salt stored in the
blob header. On a laptop at production memory cost, each derivation took
~120–300ms. A 1000-file push cost 2–4 minutes of CPU in key derivation
alone, far more than the actual file I/O. The explicit "benchmark before
1.0" comment in `crypto.py` flagged this as known tech debt.

## The roadmap proposal was broken

`docs/ROADMAP.md` Track 1C originally read:

> Cache derived keys — key an LRU by `(passphrase-hash, salt)` so repeated
> derivations in push/pull/gc reuse the key

This doesn't work. `crypto.encrypt` calls `os.urandom(16)` for each blob's
salt. An LRU keyed on `(passphrase-hash, salt)` has an effectively 0%
hit rate — every encrypt is a fresh random salt, every decrypt reads a
unique salt from the blob header, and same-process decryption of the
same blob never happens in steady-state push/pull/gc.

The `/plan-ceo-review` caught this first-pass. The fix required a
different architecture, not caching.

## What v2 does

Mirror the pattern age, restic, and rclone use:

1. A single 16-byte `root_salt` is generated once at first-device init
   and stored unencrypted at the storage root in `mm-crypto-init`.
2. `master_key = Argon2id(passphrase, root_salt, memory_cost, ...)`
   runs **once per process** and is cached in a module-level dict.
3. Per-file: `file_key = HKDF-SHA256(master_key, per_file_salt,
   info=b"mm-file-v2", length=32)`. HKDF is ~microseconds.
4. AES-256-GCM as before, with random per-file nonce. The salt in the
   blob header is now HKDF input rather than Argon2 input.

### Blob formats

```
v2 file blob:
  [version=0x02:1][salt:16][nonce:12][ciphertext+gcm_tag]

mm-crypto-init (at storage root, unencrypted):
  [version=0x02:1][argon2_memory_kb:4 BE][root_salt:16][keycheck_blob:*]

  keycheck_blob is itself a v2 blob containing b"mm-keycheck-v1",
  encrypted under the master_key derived from (passphrase, root_salt,
  argon2_memory_kb). On second-device init we decrypt keycheck_blob to
  verify the user's passphrase matches the first-device passphrase
  before writing any local state.
```

### Key state machine

```
          ┌─────────────────┐
          │ process starts  │
          └────────┬────────┘
                   │
                   ▼
     ┌───────────────────────────┐
     │  fetch_crypto_init()      │
     │  tri-state result:        │
     │    ok / missing / corrupt │
     └───────────────────────────┘
           │       │         │
      ok   │       │ missing │ corrupt
           ▼       ▼         ▼
       ┌───────┐ ┌───────┐ ┌───────┐
       │verify │ │bootst-│ │refuse │
       │keych- │ │rap new│ │with   │
       │eck    │ │salt + │ │action-│
       └───┬───┘ │kcheck │ │able   │
           │     └───┬───┘ │error  │
           │         │     └───────┘
           │         │
           ▼         ▼
     ┌───────────────────┐
     │ set_crypto_       │
     │ session(root_salt,│
     │ argon2_memory_kb) │
     └─────────┬─────────┘
               │
               ▼
     ┌───────────────────┐
     │ load_master_key() │  ← Argon2 runs ONCE per process per unique
     │ cache by          │     (pw, root_salt, memory_kb) tuple
     │ sha256(pw)||salt  │
     │ ||memory_kb       │
     └─────────┬─────────┘
               │
               ▼
     ┌───────────────────┐
     │ encrypt/decrypt   │  ← HKDF per file, microseconds
     │ via HKDF per file │
     └───────────────────┘
```

## Design decisions worth recording

### 1. Store `argon2_memory_kb` alongside `root_salt`, not per-device

If one device has `[crypto].argon2_memory_kb = 65536` and another has
`32768`, they derive different master_keys from the same passphrase and
root_salt, and blobs one writes become unreadable by the other. v1 had
this latent trap but masked it because each blob carried its own salt
and each encrypt/decrypt re-derived from scratch using whatever
`memory_kb` was passed.

v2 pins the value in `mm-crypto-init`. Local config's field is a seed
used only during first-device bootstrap; all subsequent commands read
the authoritative value from storage.

### 2. Atomic create-only primitive via `os.link`

`os.open(..., O_CREAT|O_EXCL|O_WRONLY)` gives exclusivity but not
atomicity to concurrent readers — a reader can see a zero-or-partial
file during write. A crash mid-write leaves a truncated blob that every
subsequent `fetch_crypto_init` would classify as corrupt. Codex flagged
this on `/plan-eng-review` pass 2.

Fix: write to a temp file, fsync, then `os.link(tmp, target)`.
`os.link` is atomic AND fails `EEXIST` if the target already exists.
Both properties with one primitive, no partial-file window.

### 3. iCloud is not a cross-device coordination primitive

`put_exclusive` handles the same-filesystem race (two mm inits on the
same Mac, seconds apart). It does NOT handle the cross-device race:
two Macs bootstrap nearly simultaneously, iCloud lets both succeed
locally, then reconciles later by renaming one to `mm-crypto-init 2`.

Convergence strategy: every command starts with `fetch_crypto_init`,
which scans for iCloud conflict copies, picks the deterministic winner
(lex-smallest `root_salt`), canonicalizes it via `os.rename`, and
deletes the losers. All devices converge on the same winner
deterministically as iCloud replicates the reconciled state.

Additional safety: each device's local config stores `root_salt_fp`
(a fingerprint of the `root_salt` it was initialized against). Every
command compares `root_salt_fp` against the current storage fingerprint
and refuses on mismatch — catching the "I pushed blobs under salt_A,
but iCloud reconciled to salt_B, and future blobs would be unreadable"
failure mode.

### 4. No v1 back-compat

Mind Meld is pre-release. There are no v1 blobs in any user's storage
(declared in conversation with the project owner). v2 `decrypt`
recognizes the v1 version byte (`0x01`) and fails loudly with "v1 blob
found — should not appear in any user's storage; please file a bug."

Dropping v1 back-compat halved the test surface and killed a migration
path we'd never use.

### 5. No `CryptoContext` dataclass

An earlier draft threaded `CryptoContext(master_key, format_version)`
through all 9 `cli.py` function signatures. Codex argued this was
architecture churn for a caching problem — the perf fix belongs inside
`crypto.py` as module-level state, not smeared across the CLI.

Adopted: `_MASTER_KEY_CACHE` and `_SESSION` live in `crypto.py`,
`set_crypto_session()` pins the current process's root_salt and
memory_kb, existing `(passphrase, memory_kb)` signatures in `cli.py`
are unchanged.

### 6. Keyring write is the LAST step of `mm init`

Previously `store_passphrase_in_keyring` ran before any remote
validation. A typo'd passphrase on a second-device init would land
in the OS Keychain and then every subsequent command would silently
pull the typo. Moved to after the keycheck verify passes.

## What we did NOT do (and why)

- **No `master_salt_fingerprint` in every manifest.** Earlier draft
  added this for defense-in-depth. Codex argued it duplicated global
  key metadata into every manifest and added refusal logic where the
  project's ethos is recovery. Dropped in favor of `root_salt_fp` in
  local config only.
- **No `key_scheme` byte reserved in v2 blob format.** Earlier draft
  added a 1-byte "wrapping scheme" field for future passphrase rotation.
  Codex argued this was speculative future-proofing on a pre-1.0 perf
  fix. Dropped. If rotation ships post-1.0, format v3 is cheap.
- **No committed benchmark baseline JSON.** Earlier draft suggested a
  pytest-benchmark module with committed baseline. Codex argued
  cross-machine timing baselines are process theater. Dropped in favor
  of an ad-hoc script; numbers paste into PR descriptions.
- **No `CryptoContext` dataclass** (see decision 5 above).

## Follow-up TODOs (captured in `docs/TODOS.md`)

- `mm diag` subcommand to dump non-secret crypto state for post-hoc
  debugging. A GCM tag mismatch error after v2 has three possible
  causes (wrong pw, wrong root_salt, corrupt blob) and users deserve
  a self-triage command.
- `mm init --force` guard to require explicit opt-in for re-init on
  existing storage. After v2, accidentally re-initing destroys all
  existing blobs' readability.
- `mm rekey` passphrase rotation — requires format v3 with a wrapping
  scheme. Post-1.0 feature.
- Roadmap coordination note: Track 1C touches `storage/local.py`
  (`put_exclusive`, regex fix), which Track 1D also touches. The
  declared "parallel-safe within Group 1" is no longer accurate.
