# Store v3 — fleet, envelopes, records and layout

Read BEFORE editing:

- `src/mind_meld/store/envelope.py` — `FleetKeys` / `RecordContext` / `create_fleet` / `unlock_fleet` / `seal_part` / `open_part` / `StoreError`
- `src/mind_meld/store/format.py` — `Entry` / `Record` / `IncompleteParts` / `host_fingerprint` / `encode_commit` / `decode_commit` / `encode_snapshot` / `decode_snapshot` / `encode_presence` / `decode_presence` / `seal_record` / `open_record`
- `src/mind_meld/store/layout.py` — `RecordName` / `KeyClassification` / `record_key` / `parse_record_key` / `classify_keys` / `group_record_names`
- `src/mind_meld/crypto.py` — `derive_key`

Tests: `tests/test_store_format.py` (T01–T17), `tests/test_crypto.py`,
`tests/test_module_boundaries.py`, `tests/test_docs_routing.py`.

Track 71C supplies inert primitives for MVP-1 [O4/O5](../SPEC.md#selected-target-acceptance).
It does not wire the CLI, acquire OS identity, walk sources, publish files,
apply records, manage history/DB state, change keyrings, integrate Syncthing or
migrate storage. Portable tests do not establish native/platform, wheel CI,
operator sync, replication-simulator or live-soak acceptance.

## Ownership and trust

`store/__init__.py` has no eager imports. `envelope.py` calls only the existing
`crypto.derive_key` primitive and installed cryptography primitives;
`format.py` uses `envelope.py`; `layout.py` uses only the standard library.
Importing `crypto` transitively loads `errors`, `safety`, `fsutil` and the
legacy `storage` package; none has import-time side effects. The package
imports no CLI, scanner or lifecycle module. An AST gate checks every store
module's direct imports (no CLI; only `store`, `crypto` and `errors`), and a
fresh-process test proves the CLI, manifest, config, devices and identity
modules stay unloaded.

```text
untrusted fleet bytes -> schema/limits -> Argon2 -> keycheck -> verified keys
untrusted file names  -> strict layout -> expected record/part identity
logical bytes P -> SHA256(P) -> 8 MiB chunks -> wrapper -> gzip -> AES-GCM
present parts -> authenticate/bound -> compare wrappers -> incomplete
                                                     -> assemble/hash/decode
```

Create or unlock returns an explicit `FleetKeys`, deriving the master once
and all four subkeys once. Keep it local to the caller; there is no v3 global
secret cache. Keys and salt are omitted from its repr. Only create/unlock
construct verified keys; manufacturing a dataclass does not verify a fleet.

Every envelope open requires keyword-only `expected=RecordContext(...)` and
explicit verified keys. Build that context from the verified fleet ID and
independently parsed names/caller identity. Never bootstrap it from decrypted
headers, presence counters or an unverified fleet. Decoders check payload
identity against that context, but do not authorize filesystem operations.
Authenticated data is a fleet member's claim, not permission to apply/delete.

## Fleet header and keys

The canonical writer emits UTF-8 JSON with sorted keys, compact separators,
`ensure_ascii=False`, `allow_nan=False`, no BOM and no trailing newline.
Current readers accept equivalent whitespace/key order, but reject duplicate
keys at every depth, unknown/missing fields, malformed UTF-8, nonfinite
numbers, boolean integers and parser recursion failures. All JSON strings
and keys must be Unicode scalars encodable as UTF-8: escaped lone surrogates
fail, valid supplementary characters survive, and NFC/NFD are never normalized.

`fleet.json` has exactly:

| Field | Wire value and reader |
|---|---|
| `format` | Integer 3; future versions refuse before assuming their schema |
| `fleet_id` | 32 lowercase hex characters: 16 random bytes; context and HKDF salt |
| `created_at` | Timestamp (profile below); authenticated provenance, retained in keys for Track 80A fleet lifecycle |
| `kdf` | Exactly `name="argon2id"`, `memory_kb`, `time`, `parallelism`; unlock consumes all parameters |
| `salt` | Strict, canonically padded standard base64 of 16 random bytes; Argon2 salt |
| `keycheck` | Strict standard base64 of the keycheck envelope below; unlock verifies it |

Every v3 timestamp (`created_at`, `registered`, `last_active`) uses one RFC
3339 profile: `YYYY-MM-DDTHH:MM:SS[.f{1,6}](Z|±HH:MM)` with ASCII digits,
uppercase `T`, hours 00–23 and a mandatory offset; the calendar date must be
valid. The pattern is checked before `datetime.fromisoformat`, whose accepted
set differs by Python version (3.14 takes `T24:00`) and admits any separator
character, so every supported reader reaches the same verdict.
`datetime.now(timezone.utc).isoformat()` (default separator and timespec)
emits this profile; offsets with seconds and `timespec="minutes"`/`"hours"`
do not, so writers must normalize before encoding.

Writer defaults: 65536 KiB memory, time cost 3, parallelism 1. Reader bounds:
memory 1024..262144 KiB, time 1..10, parallelism 1..16, and memory at least
8 × parallelism. Prevalidate all of these before native Argon2 allocation.
Unsupported algorithms refuse without deriving; invalid current parameters
are `invalid_fleet`. Native derivation failure is `resource_limit`, with no
advice to change v2 config or reinitialize an existing fleet.

The master is Argon2id(passphrase UTF-8, raw salt, specified parameters), 32 bytes.
The primitive `crypto.derive_key(passphrase, salt, memory_kb=65536, *,
time_cost=3, parallelism=1)` retains all legacy defaults, cache keys and
session behavior. `crypto.FORMAT_VERSION` remains **0x02**. Its readers and
crypto-init still refuse 0x03–0x0F, even a one-byte input. V3 never calls
legacy encrypt/decrypt/load-master-key or accesses its session/cache.

For each kind `commit`, `snapshot`, `presence`, `keycheck`, derive:
HKDF-SHA256(master, length=32, salt=raw fleet_id,
info=`b"mm-store-v3/" + kind.encode("ascii")`). All four domains differ.

Keycheck plaintext is exactly `b"mm-keycheck-v3"`. Its AAD is
`b"mm-fleet-v3\0"` followed by canonical JSON of **every header field except
keycheck**. Seal under the keycheck subkey using the same v3 envelope, with
a random 12-byte nonce. Open authenticates before gzip decoding, caps output
at 64 bytes and requires the sentinel. Altered header/KDF/salt/fleet identity
or wrong secret refuses; failure never creates/replaces a fleet.

## Envelope and part identity

```text
[03:1 byte][random nonce:12 bytes][AES-256-GCM ciphertext][full tag:16 bytes]
```

Compress before encryption: one gzip member, compression level 9, mtime=0,
OS=255. Version is inspected before assuming layout: 0x04–0x0F is
`unsupported_format`; empty, 0x01/0x02, other garbage or a truncated current
layout is invalid for this explicit v3 API. No auto-detection or fallback.
Check sealed length and context ranges before AES-GCM, authenticate before
decompression, and reject truncated gzip, concatenated members, trailing
compressed junk or excessive expansion. The decoder materializes at most
the allowance plus one byte used to detect overflow.

Record AAD is `b"mm-record-v3\0"` + canonical JSON bytes of this array:
`[fleet_id, device_id, kind, seq, part, nparts]`. Fleet ID is lowerhex32;
device ID is lowerhex8; kind is `commit`, `snapshot` or `presence`; indices
are integers, never bool. Commit seq starts at 1; snapshot seq starts at 0;
both stop at 999999999999. Part indices are 1-based, count 1..512.
Presence always uses seq=0, part=1, nparts=1. Changing any bound identity
fails authentication (noncanonical contexts fail earlier).

Renaming, moving between devices/kinds or renumbering fails. The record
family in a commit or snapshot name is **not** in the associated data: a file
renamed to another fleet, device, kind, seq, part or part count fails
authentication, and the payload digest (not the family) is the record identity
for de-duplication and fork comparison. Binding the family is assigned to Track
80A with its version-byte change. Replaying a whole authentic record **at its
original slot** can still decode. Later stateful apply/lifecycle work owns
freshness, chain equality and fork handling.

## Logical record framing

A frame is `[length:u32 big endian][exact length bytes]`. First frame is the
JSON header. Each subsequent entry is JSON metadata plus a raw content frame;
a tombstone omits the content frame. A live empty file has a zero-length
content frame. Declared entry count is exact; reject missing/extra frames,
truncation, oversized lengths, invalid JSON and any trailing bytes. Canonical
writers preserve entry order. Readers accept equivalent JSON encodings;
logical payload hashes always use **original bytes**, never reserialization.

Commit header has exactly `fleet_id, device_id, host, seq, prev, created_at,
mm_version, entries`. Snapshot adds exactly `cursors`. Header fleet/device/seq
must equal expected context; codec selection supplies kind. `created_at`
uses the timestamp profile; `mm_version` is a nonempty scalar string.
`host` is lowerhex32: first 16 raw digest bytes of
SHA256(raw16-byte fleet ID || trimmed supplied machine-id UTF-8).
`host_fingerprint` accepts a nonempty supplied machine ID and performs no
Linux machine-id or macOS UUID acquisition.

Commit seq1 uses `prev=null`; later commits require lowerhex64 SHA256 of the
exact previous **logical commit payload**. Snapshot seq0 uses `prev=null`;
nonzero snapshots require the commit-at-seq payload digest, not a previous
snapshot hash. Codecs validate/return these facts without comparing chains.
Snapshot `cursors` maps canonical device IDs to nonnegative filename-range
sequences; an empty map is valid.

Entry metadata has exactly `source, path, vv, sha, size, mtime, deleted`;
snapshot may also have boolean `held` (absence means false). Commit forbids
that field. Source is a nonempty safe single component. Path is nonempty
relative POSIX: no empty/dot/dotdot segments, leading slash, NUL, backslash or
drive prefix. Source follows the same constraints plus no slash. Preserve
Unicode identity exactly; display sanitization and filesystem guards remain
future caller obligations.

`vv` is a nonempty map of lowerhex8 device IDs to positive counters at most
2^63-1. Live entries need lowerhex64 plaintext SHA256, nonnegative integer
size exactly equal to content length, and finite nonnegative numeric mtime
(not bool). Verify content SHA after decoding. Tombstones need `sha=null`,
`size=0`, `mtime=null`, `deleted=true`, no content, and retain source/path/vv.
Commits reject duplicate (source,path). Snapshots allow one unheld version per
path and distinct held versions, including held tombstones, but reject any
duplicate (source,path,vv), independent of vv key order. Count every version;
never compare vector clocks, reorder entries, prune or drop tombstones.

Presence is one JSON object (no frame prefix), with exactly `device_id,
device_name, os, mm_version, host, syncthing_id, registered, last_active,
published_seq, snapshot_seq, applied`. Identity/host shapes match above;
device must match expected context. Other strings are nonempty UTF-8 scalars;
registered/last_active use the timestamp profile. Counters/applied peer
sequences are nonnegative integers in the filename range, and
snapshot_seq ≤ published_seq. Use the same single-part wrapper/envelope;
published/snapshot counters never supply AAD. Successive or old authentic
records at that presence path may decode; no freshness is asserted. Device
name, OS and Syncthing ID remain inert data, never display/path authority.

## Multipart and limits

Encode the complete logical framed payload **P**, hash its exact uncompressed
bytes once, then split it into chunks of at most 8 MiB **before** compression.
Every authenticated part plaintext, including a single part, is:

```text
[SHA256(P):32 raw bytes][len(P):u64 big endian][uncompressed chunk]
```

The wrapper is outside P and its own digest. Repeated digest/total must agree.
Count equals max(1, ceil(total/8 MiB)); every nonfinal chunk is exactly 8 MiB;
final length is total − 8 MiB × (count−1), also exactly 8 MiB at exact multiples.
Validate every present part, including partial input, before reporting missing
indices. Duplicates, mixed identity/count/digest/total, bad lengths/tags or
other invalid present parts are errors even when other parts are absent.
Reordered complete sets assemble identically. Verify total length and final
SHA256(P), then decode the complete record before returning any payload.
Hybrid parts with individually valid tags/content hashes still refuse.
Callers group present parts by `(device, kind, seq, family)`
(`layout.group_record_names`) and pass each family's parts to its own
`open_record` call. Families at one slot are distinct records, not a fork by
themselves, and one family name has exactly one part count: parts of one family
that disagree on the count refuse as `invalid_parts`. A caller judges a family
complete from its names (every index of one count present) and opens only
complete families; layout flags no family as a fork.

| Limit | Bytes/count |
|---|---|
| Uncompressed chunk | 8 × 1024 × 1024 |
| Sealed part, including all overhead | 8 MiB + 64 KiB |
| Logical record, including frames | 4 × 1024^3 (4 GiB) |
| Parts / missing-index enumeration | 512 |
| Header JSON | 1 MiB |
| Each metadata JSON / presence JSON | 64 KiB |
| Fleet JSON | 16 KiB |
| Entries | 1,000,000, also bounded by available frame prefixes |

Lengths/counts are checked before decryption, slicing, expansion or assembly
as applicable. A u32 frame cannot hold 4 GiB content; the whole-record ceiling
includes frame overhead. These byte APIs use O(P) memory and may temporarily
copy the payload/content during framing/validation/assembly. They make no
constant-memory or maximum-size operational guarantee. `seal_record` yields
sealed parts incrementally instead of a second full sealed list. Disk spooling
and streaming framing are outside this Track.

## Layout and classification

Canonical record keys only:

```text
fleet.json                          # plaintext verified header
.stignore                           # control file; never decrypt
dev/<lowerhex8>/log/<12-digit seq>.<lowerhex8 family>.enc
dev/<lowerhex8>/snap/<12-digit seq>.<lowerhex8 family>.enc
dev/<lowerhex8>/log|snap/<seq>.<family>.<part>of<count>.enc
dev/<lowerhex8>/presence.enc
```

The family is exactly 8 lowercase hex characters, fresh for each publication of
a commit or snapshot; presence has none. All parts of one publication share one
family and one part count, and a slot `(device, kind, seq)` can hold several
families. Single part has no part suffix. Multipart needs 2..512 parts, unpadded
decimal indices and 1 ≤ part ≤ count. A name without a family is foreign. No
traversal, case/width changes or normalization.
Construct v3 record keys only with `record_key`; use its constants for controls.
`storage/keys.py` remains the validated builder for the 1.x namespace; neither
namespace allows raw f-strings at call sites.
`parse_record_key` returns `RecordName` or None; controls are recognized by
`classify_keys`. Supply file keys, with directories represented as directory
keys/trailing slash. No parser performs filesystem I/O or discovers file type.

`classify_keys` preserves supplied order and returns records, controls,
foreign names and fork names. `foreign_count` includes every fork already;
never sum forks into it again. Only canonical fleet/commit names with
`.sync-conflict-YYYYMMDD-HHMMSS-<modifiedBy>` inserted before their extension
are fork signals. Date/time must be valid; modifiedBy is 1..64 ASCII
alphanumeric characters, not necessarily a full Syncthing device ID.
Snapshot/presence copies, temps, malformed near matches and directory keys
remain foreign without a new fork policy. Future callers must sanitize names
before displaying them and preserve evidence; only the forked device refuses —
peers warn and keep applying (store-v2 §6, §9).

## Public API

| Owner / API | Inputs and result |
|---|---|
| envelope: `create_fleet` | Passphrase; optional created_at and bounded KDF kwargs; returns `(fleet_json: bytes, FleetKeys)`; no publication |
| envelope: `unlock_fleet` | Untrusted fleet bytes and passphrase; returns verified `FleetKeys` or `StoreError` |
| envelope: `RecordContext` | Independent fleet/device/kind/seq, default part=1/nparts=1; validates canonical dimensions; `associated_data()` returns exact tuple bytes |
| envelope: `seal_part` / `open_part` | Bytes, verified keys, required keyword-only expected context; bounded bytes result. Low-level part plaintext includes the caller's wrapper; format APIs manage it |
| format: `encode_commit` / `encode_snapshot` | Exact header dict, sequence of `Entry(metadata, content)`, keyword-only expected context; returns logical framed bytes |
| format: `decode_commit` / `decode_snapshot` | Logical bytes and expected context; returns `Record(header, tuple(entries))` with all facts validated |
| format: `encode_presence` / `decode_presence` | Presence dict/bytes and fixed expected context; returns canonical bytes/validated dict |
| format: `host_fingerprint` | Explicit fleet ID and supplied machine ID; returns lowerhex32 |
| format: `seal_record` | Logical bytes, keys, expected **anchor** part1/count1; validates record and yields `(full_part_context, sealed_bytes)`; work occurs on iteration |
| format: `open_record` | Iterable of `(independent full_part_context, sealed_bytes)`, keys, expected anchor part1/count1, optional known `nparts`; returns original validated P bytes or `IncompleteParts` |
| layout: `record_key` | Device/kind/seq, part/count defaults 1, keyword `family` (required for commit and snapshot, rejected for presence); canonical str, or safe ValueError for invalid caller components |
| layout: `parse_record_key` / `classify_keys` | Untrusted key/file-key iterable; `RecordName | None` (carries `family`) / `KeyClassification` facts |
| layout: `group_record_names` | `(key, RecordName)` pairs such as `KeyClassification.records`; dict keyed by `(device, kind, seq, family)` in sorted order, each value the pairs in supplied order. Pure: no I/O, no completeness judgement |

An unknown empty log/snapshot set returns `IncompleteParts(None, None)`.
A known count returns exact missing indices; presence intrinsically knows
count1. No incomplete result carries applicable plaintext. Use the complete
payload with the selected decoder for structured access; `open_record` already
validates it, so this explicit second decode repeats schema/content checks.

## Errors

`StoreError.category` is stable; its fixed text contains problem, possible
cause, remedy and this reference. No secret, content, raw machine ID or
peer-controlled text is interpolated. Preserve exceptions for invalid complete
data and the distinct `IncompleteParts` result for valid missing input.

| Category/result | Safe cause and remedy |
|---|---|
| `unsupported_format` | Future format/algorithm; use a compatible reader |
| `invalid_fleet` | Malformed current header/KDF, or empty/unencodable passphrase input; check the passphrase, then preserve the header and inspect the fleet |
| `authentication` | Wrong secret, expected identity or altered bytes; these are indistinguishable. Check passphrase/context and preserve inputs |
| `invalid_payload` | Malformed compression/framing/schema/content hash; preserve inputs and inspect the producer |
| `invalid_parts` | Mixed/duplicate/misnamed dimensions, wrapper or whole-record digest disagreement, or a builder call with a missing, malformed or presence family; preserve inputs and inspect identities |
| `resource_limit` | Bound exceeded or native derivation unavailable; inspect record/producer/machine resources, do not truncate or reinitialize |
| `IncompleteParts` | Valid absent pieces; wait for remaining parts |

Invalid builder arguments raise safe `ValueError` with `invalid_parts` guidance,
and each family mistake (missing for a commit or snapshot, not 8 lowercase hex,
given for presence) has its own problem, cause and fix text: the
standard-library-only layout has no crypto dependency. Parse/classify
untrusted names instead; foreign names are facts, not exceptions. No decoder
repairs storage, touches keyrings/state DB or silently creates a new fleet.

## In-memory caller example and one-time timing

Python 3.11+ is required. From the checkout, start the TTHW clock when opening
these instructions. Follow the existing environment bootstrap/check entry point:

```sh
./bin/check tests/test_store_format.py tests/test_crypto.py tests/test_module_boundaries.py tests/test_docs_routing.py
```

Then copy this complete example. It uses synthetic values and real crypto,
with no storage/keyring calls. Stop the clock after round trip and wrong-context
refusal. Record observed elapsed time, whether the environment was warm, and
any bootstrap/network/fix time; target <5 minutes, without claiming an unmeasured
cold setup or native/transport acceptance. This is a one-time documentation
check, not a new recurring release/telemetry gate.

<!-- example:start -->
```sh
./.venv/bin/python - <<'PY'
import hashlib
from dataclasses import replace
from mind_meld.store import envelope, format as codec, layout

passphrase = "synthetic example passphrase"
fleet_bytes, created_keys = envelope.create_fleet(passphrase)
keys = envelope.unlock_fleet(fleet_bytes, passphrase)
assert keys == created_keys
device = "a1b2c3d4"
family = "0badf00d"  # the publisher draws a fresh one per publication
anchor = envelope.RecordContext(keys.fleet_id, device, "commit", 1)
content = b"x" * (codec.MAX_PART_BYTES + 1)
header = {
    "fleet_id": keys.fleet_id, "device_id": device,
    "host": codec.host_fingerprint(keys.fleet_id, "synthetic-machine"),
    "seq": 1, "prev": None, "created_at": keys.created_at,
    "mm_version": "2.0.0", "entries": 1,
}
entry = codec.Entry({
    "source": "claude", "path": "notes/example.md", "vv": {device: 1},
    "sha": hashlib.sha256(content).hexdigest(), "size": len(content),
    "mtime": 0, "deleted": False,
}, content)
payload = codec.encode_commit(header, [entry], expected=anchor)
named = []
for part_context, sealed in codec.seal_record(payload, keys, expected=anchor):
    name = layout.record_key(device, "commit", 1, family=family,
                             part=part_context.part, nparts=part_context.nparts)
    named.append((name, sealed))

def expected_parts(named_inputs):
    # Group by (device, kind, seq, family); each family opens on its own.
    named_inputs = list(named_inputs)
    records = [(name, layout.parse_record_key(name)) for name, _ in named_inputs]
    assert all(parsed is not None for _, parsed in records)
    assert len(layout.group_record_names(records)) == 1  # one slot, one family here
    parts = []
    for (_, parsed), (_, sealed) in zip(records, named_inputs):
        expected = envelope.RecordContext(keys.fleet_id, parsed.device_id,
            parsed.kind, parsed.seq, parsed.part, parsed.nparts)
        parts.append((expected, sealed))
    return parts

assert codec.open_record([], keys, expected=anchor) == codec.IncompleteParts(None, None)
assert codec.open_record(expected_parts(named[:1]), keys, expected=anchor) == (
    codec.IncompleteParts((2,), 2)
)
opened = codec.open_record(expected_parts(reversed(named)), keys, expected=anchor)
assert opened == payload
record = codec.decode_commit(opened, expected=anchor)
assert record.entries == (entry,)

renamed = []
for name, sealed in named:
    parsed = layout.parse_record_key(name)
    renamed.append((layout.record_key(device, "commit", 2, family=family,
        part=parsed.part, nparts=parsed.nparts), sealed))
try:
    codec.open_record(expected_parts(renamed), keys, expected=replace(anchor, seq=2))
except envelope.StoreError as error:
    assert error.category == "authentication"
else:
    raise AssertionError("different expected sequence must refuse")
print("Round trip, incomplete parts and wrong-context refusal: OK")
PY
```
<!-- example:end -->

Independent vectors live in T01 (Argon2/HMAC-HKDF, fleet AAD and keycheck),
T04 (tuple AAD, fixed nonce/ciphertext), T06 (literal framed commit and digest)
and T14 (raw-fleet host fingerprint). T08–T10 exercise real 8 MiB boundaries
and limit claims without allocating a 4 GiB fixture. Keep these vectors and
all legacy regression tests intact when changing this protocol.
