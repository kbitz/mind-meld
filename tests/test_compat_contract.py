"""Release tripwires for storage-format gates.

The 1.0 reader fixtures are immutable. An mm still refuses storage written in a
format it cannot read.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mind_meld import crypto, devices, events, manifest, skill_link
from mind_meld.skills.retro_fleet import aggregator
from mind_meld.storage import keys
from mind_meld.storage.local import LocalBackend

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures/compat_1_0"
CONTRACT = "docs/invariants/auto-upgrade.md#storage-format-gates"
FORMAT_FAILURE = (
    f"{CONTRACT}: this is a MAJOR; unreadable formats require the crypto-init older-side gate."
)
READER_FAILURE = f"{CONTRACT}: a 1.x reader must read what 1.0 wrote."
PASSPHRASE = "compat-1.0-public-test-passphrase"


def _compat_read(reader, *args, **kwargs):
    try:
        return reader(*args, **kwargs)
    except Exception as exc:
        pytest.fail(f"{READER_FAILURE} {type(exc).__name__}: {exc}")


def test_format_constants():
    actual = {
        "crypto": [
            crypto.FORMAT_VERSION,
            crypto.FORMAT_VERSION_MAX,
            crypto.SALT_LEN,
            crypto.NONCE_LEN,
            crypto.ROOT_SALT_LEN,
            crypto.MEMORY_KB_FIELD_LEN,
            crypto.HKDF_INFO,
            crypto._KEYCHECK_PLAINTEXT,
        ],
        "events": events.EVENTS_SCHEMA_VERSION,
        "conflicts": [
            manifest.CONFLICT_INFIX,
            manifest.CONFLICT_V0_PREFIX,
            manifest.CONFLICT_V1_MARKER,
        ],
        "storage": [
            keys.MANIFESTS_PREFIX,
            keys.DATA_PREFIX,
            keys.DEVICES_PREFIX,
            keys.CRYPTO_INIT_KEY,
        ],
        "manifest": set(manifest.load_manifest(b'{"sources":{},"tombstones":{}}')),
    }
    assert actual == {
        "crypto": [2, 15, 16, 12, 16, 4, b"mm-file-v2", b"mm-keycheck-v1"],
        "events": 2,
        "conflicts": [".sync-conflict-", "v0-", "v1"],
        "storage": ["manifests/", "data/", "devices/", "mm-crypto-init"],
        "manifest": {"sources", "tombstones"},
    }, FORMAT_FAILURE


def test_device_registry_writer_fields(tmp_path):
    backend = LocalBackend(tmp_path / "storage")
    devices.register_device(backend, "compat", "Compat")
    devices.update_last_seen(backend, "compat")
    row = json.loads(backend.get(keys.device_key("compat")))
    assert set(row) == {
        "device_id",
        "device_name",
        "registered",
        "last_seen",
        "last_seen_version",
    }, FORMAT_FAILURE


def test_frozen_crypto_reader(tmp_path):
    backend = LocalBackend(tmp_path / "storage")
    backend.put(keys.CRYPTO_INIT_KEY, (FIXTURES / "mm-crypto-init").read_bytes())
    fetched = _compat_read(crypto.fetch_crypto_init, backend)
    assert fetched.status == "ok", READER_FAILURE
    assert fetched.argon2_memory_kb == 1024, READER_FAILURE
    master = _compat_read(
        crypto.load_master_key, PASSPHRASE, fetched.root_salt, fetched.argon2_memory_kb
    )
    _compat_read(crypto.verify_passphrase, master, fetched.keycheck_blob)
    crypto.set_crypto_session(fetched.root_salt, fetched.argon2_memory_kb)
    assert _compat_read(crypto.decrypt, (FIXTURES / "blob.enc").read_bytes(), PASSPHRASE, 1024) == (
        b"Mind Meld 1.0 compatibility fixture.\n"
    ), READER_FAILURE


def test_frozen_manifest_reader():
    row = _compat_read(manifest.load_manifest, (FIXTURES / "manifest.json").read_bytes())
    assert row["sources"]["notes"]["files"]["note.md"]["sha256"] == (
        hashlib.sha256(b"Mind Meld 1.0 compatibility fixture.\n").hexdigest()
    ), READER_FAILURE
    assert row["tombstones"]["notes:removed.md"]["deleted_by"] == "compat", READER_FAILURE


def test_frozen_event_readers():
    push = json.loads((FIXTURES / "mm-push.json").read_bytes())
    aggregate = _compat_read(
        aggregator.aggregate_pushes,
        [push],
        since=datetime(2026, 9, 21, tzinfo=timezone.utc),
        until=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )
    assert aggregate.push_events == 1, READER_FAILURE
    assert aggregate.devices_with_pushes == {"compat"}, READER_FAILURE
    host = json.loads((FIXTURES / "host-usage-snapshot.json").read_bytes())
    accepted = _compat_read(aggregator._accept_host_usage_snapshot, host)
    assert not isinstance(accepted, aggregator.HostReject), READER_FAILURE
    assert accepted.lifetime_by_family == host["hosts"], READER_FAILURE
    assert accepted.tokens_by_day == host["tokens_by_day"], READER_FAILURE
    assert accepted.counter_semantics == "disjoint-v1", READER_FAILURE


def test_frozen_device_reader(tmp_path):
    backend = LocalBackend(tmp_path / "storage")
    raw = (FIXTURES / "device.json").read_bytes()
    backend.put(keys.device_key("compat"), raw)
    assert _compat_read(devices.list_devices, backend) == [json.loads(raw)], READER_FAILURE


def test_empty_store_installs_running_package():
    source = skill_link._resolve_retro_skill_src()
    store = skill_link._skill_store_dir()
    assert not store.exists()
    skill_link._publish_skill_store(source)
    assert (store / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
