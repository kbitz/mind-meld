"""Release tripwires for docs/invariants/auto-upgrade.md#compatibility-1x.

The 1.0 reader fixtures are immutable. The CLI golden can grow in a MINOR;
updating it requires classifying the change against the contract first.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

from mind_meld import cli, crypto, devices, events, host_usage, manifest, skill_link, token_usage
from mind_meld.skills.retro_fleet import aggregator
from mind_meld.storage import keys
from mind_meld.storage.local import LocalBackend

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures/compat_1_0"
GOLDEN = ROOT / "tests/fixtures/cli_surface_1_x.json"
CONTRACT = "docs/invariants/auto-upgrade.md#compatibility-1x"
FORMAT_FAILURE = (
    f"{CONTRACT}: this is a MAJOR; unreadable formats require the crypto-init older-side gate."
)
READER_FAILURE = f"{CONTRACT}: a 1.x reader must read what 1.0 wrote."
PASSPHRASE = "compat-1.0-public-test-passphrase"
runner = CliRunner()


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


def test_closed_vocabularies():
    assert get_args(host_usage.HostFamily) == ("claude", "codex", "grok", "other"), FORMAT_FAILURE
    assert aggregator._HOST_FAMILIES == {"claude", "codex", "grok", "other"}, FORMAT_FAILURE
    assert token_usage.TOKEN_FIELDS == ("input", "cache_create", "cache_read", "output"), (
        FORMAT_FAILURE
    )
    assert events.HOST_USAGE_TOKEN_SOURCES == ("codex", "grok"), FORMAT_FAILURE


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


def cli_surface() -> dict:
    """Framework help/completion and defaults are outside the stable surface."""
    surface = {}

    def walk(command, path):
        params = {}
        for param in command.params:
            if set(param.opts) & {"--help", "--install-completion", "--show-completion"}:
                continue
            record = {
                "kind": param.param_type_name,
                "names": [*param.opts, *getattr(param, "secondary_opts", [])],
                "required": param.required,
                "is_flag": getattr(param, "is_flag", False),
                "multiple": param.multiple,
                "nargs": param.nargs,
                "type": param.type.name,
                "choices": list(param.type.choices) if hasattr(param.type, "choices") else None,
            }
            params[param.name] = record
        surface[path] = params
        for name, child in sorted(getattr(command, "commands", {}).items()):
            walk(child, name if path == "<root>" else f"{path} {name}")

    walk(get_command(cli.app), "<root>")
    return surface


def test_cli_surface_golden():
    expected = json.loads(GOLDEN.read_text())
    actual = cli_surface()
    assert "resolve" in expected and expected["<root>"], "empty CLI golden"
    assert any("--no-save" in p["names"] for p in expected["retro-fleet"].values())
    removed = set(expected) - set(actual)
    added = set(actual) - set(expected)
    changed = []
    for command in expected.keys() & actual.keys():
        removed.update(f"{command}:{p}" for p in expected[command].keys() - actual[command].keys())
        added.update(f"{command}:{p}" for p in actual[command].keys() - expected[command].keys())
        for p in expected[command].keys() & actual[command].keys():
            old, new = dict(expected[command][p]), dict(actual[command][p])
            for field in ("names", "choices"):
                old_values, new_values = old.pop(field), new.pop(field)
                if old_values is None or new_values is None:
                    if old_values != new_values:
                        changed.append(f"{command}:{p}:{field}")
                    continue
                removed.update(
                    f"{command}:{p}:{value}" for value in set(old_values) - set(new_values)
                )
                added.update(
                    f"{command}:{p}:{value}" for value in set(new_values) - set(old_values)
                )
            if old != new:
                changed.append(f"{command}:{p}")
    assert not removed and not changed, f"{CONTRACT}: MAJOR surface change: {removed}, {changed}"
    assert not added, f"{CONTRACT}: add to the golden (MINOR): {added}"


# Each README exit outcome has behavior behind it; the AST scan below is
# supplementary. References are validated so moved/deleted tests cannot silently
# leave the contract citing stale evidence. Parametrized cases count as one entry.
EXIT_EVIDENCE = {
    "0: success": "test_integration.py::TestPushPullRoundTrip::test_push_then_pull",
    "0: valid autorun refusal": (
        "test_integration.py::TestNewerStorage66A::test_refuses_before_manifest_read"
    ),
    "0: per-file pull failure": (
        "test_integration.py::TestApplyRecovery53A::test_collision_records_all_files_and_retries"
    ),
    "1: stopped": "test_integration.py::TestNewerStorage66A::test_refuses_before_manifest_read",
    "1: required maintenance": (
        "test_integration.py::TestNewerStorage66A::test_newer_manifest_arrives_before_crypto_init"
    ),
    "1: EOF or Ctrl-C abort": "test_compat_contract.py::test_resolve_abort_exit",
    "1: rejected argument value": "test_compat_contract.py::test_rejected_value_exit",
    "2: usage error": "test_compat_contract.py::test_usage_exit",
    "3: pull preflight": "test_integration.py::test_fail_mode_local_failure_only62",
    "4: partial recapture": "test_track_30a.py::test_recapture_partial_walk_exits_4",
}


def test_exit_evidence_references():
    for outcome, reference in EXIT_EVIDENCE.items():
        filename, *names = reference.split("::")
        node = ast.parse((ROOT / "tests" / filename).read_text())
        for name in names:
            node = next((n for n in node.body if getattr(n, "name", None) == name), None)
            assert node is not None, (
                f"{CONTRACT}: missing behavioral evidence for {outcome}: {reference}"
            )


def test_exit_code_ast_all_modules():
    dynamic = {
        ("cli.py", "retro_fleet_cmd", "_aggregator_main(argv)"),
        ("cli.py", "recapture", "RECAPTURE_EXIT_PARTIAL"),
        ("skills/retro_fleet/aggregator.py", "<module>", "main()"),
    }
    seen = set()
    for path in (ROOT / "src/mind_meld").rglob("*.py"):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name not in {"Exit", "exit", "SystemExit", "_exit", "quit"}:
                continue
            code = (
                node.args[0]
                if node.args
                else next((kw.value for kw in node.keywords if kw.arg == "code"), ast.Constant(0))
            )
            if isinstance(code, ast.Constant):
                assert type(code.value) is int and code.value in {0, 1, 3}, CONTRACT
                continue
            owner = parents.get(node)
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents.get(owner)
            entry = (
                path.relative_to(ROOT / "src/mind_meld").as_posix(),
                owner.name if owner else "<module>",
                ast.unparse(code),
            )
            assert entry in dynamic, f"{CONTRACT}: unclassified exit {entry}"
            seen.add(entry)
    assert seen == dynamic, f"{CONTRACT}: reclassify changed exit paths"
    assert cli.RECAPTURE_EXIT_PARTIAL == 4, CONTRACT


@pytest.mark.parametrize(
    "argv", [["autopush", "--bogus"], ["retro-fleet", "--bogus"], ["retro-fleet", "bad"]]
)
def test_usage_exit(argv):
    result = runner.invoke(cli.app, argv)
    assert result.exit_code == 2, result.output


@pytest.mark.parametrize(
    "argv", [["log", "--since", "bad"], ["log", "--format", "xml"], ["recapture", "999d"]]
)
def test_rejected_value_exit(argv):
    result = runner.invoke(cli.app, argv)
    assert result.exit_code == 1, result.output
    assert "Error:" in result.output


@pytest.mark.parametrize("interrupt", [False, True])
def test_resolve_abort_exit(tmp_path, monkeypatch, interrupt):
    local = tmp_path / "note.md"
    local.write_bytes(b"local\n")
    remote = tmp_path / "note.sync-conflict-20260922-120000-v1-peer1234.md"
    remote.write_bytes(b"peer\n")
    monkeypatch.setattr(cli, "_get_config", lambda **kw: {})
    monkeypatch.setattr(cli, "get_backend", lambda cfg: LocalBackend(tmp_path / "storage"))
    monkeypatch.setattr(cli, "get_sources", lambda cfg: [])
    monkeypatch.setattr(
        cli.resolveflow, "_find_conflict_files", lambda *a, **kw: [("notes", remote, local)]
    )
    if interrupt:

        def ctrl_c(*a, **kw):
            raise KeyboardInterrupt

        monkeypatch.setitem(typer.prompt.__globals__, "visible_prompt_func", ctrl_c)
    result = runner.invoke(cli.app, ["resolve"], input="")
    assert result.exit_code == 1, result.output
    assert "Aborted" in result.output  # punctuation belongs to the framework
    assert local.read_bytes() == b"local\n" and remote.read_bytes() == b"peer\n"


def test_resolve_help_and_skill_step4():
    result = runner.invoke(cli.app, ["resolve", "--help"])
    assert result.exit_code == 0
    assert "(n)ewer" in result.output
    skill = (ROOT / "src/mind_meld/skills/retro_fleet/SKILL.md").read_text()
    step = skill.split("## Step 4:")[1].split("## Step 5:")[0]
    assert "--no-save" not in step
