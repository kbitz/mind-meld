"""Shared pytest fixtures for Mind Meld tests.

All tests that call crypto.encrypt / crypto.decrypt need an active crypto
session. Rather than hand-wire one in every test, an autouse fixture here
sets a fixed root_salt and a low Argon2 memory cost so unit/integration tests
run fast.

Tests that specifically want to test session behavior (drift detection,
missing session, bootstrap) can call crypto.clear_crypto_session() inside
their test body and then re-configure.
"""

from __future__ import annotations

import pytest

from mind_meld import crypto

# Fixed, arbitrary 16-byte root_salt for the default test session. Not secret.
_TEST_ROOT_SALT = bytes(range(16))
# Low Argon2 cost so tests run in <1s per call. Production default is 65_536.
_TEST_MEMORY_KB = 1024


@pytest.fixture(autouse=True)
def _default_crypto_session() -> None:
    """Pin a default crypto session before each test; clear after.

    Ensures encrypt/decrypt work without per-test boilerplate. Tests that
    assert on fresh-session behavior should call crypto.clear_crypto_session()
    inside their test body to reset.
    """
    crypto.clear_crypto_session()
    crypto.set_crypto_session(_TEST_ROOT_SALT, _TEST_MEMORY_KB)
    yield
    crypto.clear_crypto_session()


@pytest.fixture(autouse=True)
def _isolate_keyring(monkeypatch) -> None:
    """Prevent tests from reading the real OS Keychain.

    get_passphrase() checks keyring first; a stale passphrase stored from a
    real mm session would override test env vars and make integration tests
    nondeterministic. Force keyring to report "not available" in every test.
    """
    monkeypatch.setattr("keyring.get_password", lambda *a, **kw: None)
    monkeypatch.setattr("keyring.set_password", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _isolate_devices_write_lock(monkeypatch, tmp_path) -> None:
    """Redirect devices/<id>.json write-lock to a per-test path.

    `update_last_seen` (and any future RMW mutator) routes through
    `mind_meld.devices._devices_write_lock()`, which flocks
    `DEVICES_WRITE_LOCK`. Without redirection, every test creates the
    sentinel in the real `~/.config/mind-meld/`. The flock itself is
    process-local so cross-test contention isn't a concern, but leaking
    files into the user's home dir is bad hygiene.

    Import devices explicitly so monkeypatch can resolve the dotted path
    even in tests (test_version, test_wheel) that don't otherwise touch it.
    """
    from mind_meld import devices as _devices

    monkeypatch.setattr(_devices, "DEVICES_WRITE_LOCK", tmp_path / "devices-write.lock")


@pytest.fixture(autouse=True)
def _isolate_identity_cache(monkeypatch, tmp_path) -> None:
    """Redirect ``mind_meld.identity.CACHE_PATH`` to a per-test path.

    ``identity.gather_local_identities`` writes
    ``~/.config/mind-meld/identity-cache.json`` on first read of a stale
    cache. Without isolation, every test that drives push tail / retro
    aggregator (test_events, test_init_events_backfill, test_retro_fleet_
    aggregator, test_integration) would pollute the user's real config
    dir AND read whatever was previously cached there — non-deterministic
    test runs.

    Pattern mirrors ``_isolate_pullhistory``: ``CACHE_PATH`` is module-
    level, read at call time inside ``locked_json_rmw``, so a single
    ``setattr`` suffices.
    """
    from mind_meld import identity as _identity

    monkeypatch.setattr(_identity, "CACHE_PATH", tmp_path / "identity-cache.json")


@pytest.fixture(autouse=True)
def _isolate_token_cache(monkeypatch, tmp_path) -> None:
    """Redirect ``mind_meld.token_usage.CACHE_PATH`` to a per-test path
    and reset all per-process warning sets.

    ``warm_token_cache_inline`` / ``gc_cache_entries`` /
    ``lock_and_get_files`` all read/write
    ``~/.config/mind-meld/session-tokens.json`` by default. Without
    isolation, every test that drives ``mm push`` / ``_run_events_tail``
    (test_integration, test_init_events_backfill, test_silent_failure_
    contract, etc.) pollutes the user's real config dir AND inherits
    whatever was cached there — non-deterministic.

    Also resets the two ``warn-once`` per-process state sets so a test
    that triggers a breadcrumb doesn't silently mute the same breadcrumb
    in a later test:
      - ``_WARNED_UNKNOWN_MODELS`` (estimate_cost path)
      - ``_WARNED_OVERSIZE_PATHS`` (iter_bounded_lines path; shared by the
        token walker and events.py's cwd + cursor readers)

    Pattern mirrors ``_isolate_identity_cache``: lazy-import inside the
    fixture body, single setattr per state.
    """
    from mind_meld import token_usage as _token_usage

    monkeypatch.setattr(_token_usage, "CACHE_PATH", tmp_path / "session-tokens.json")
    monkeypatch.setattr(_token_usage, "_WARNED_UNKNOWN_MODELS", set())
    monkeypatch.setattr(_token_usage, "_WARNED_OVERSIZE_PATHS", set())


@pytest.fixture(autouse=True)
def _isolate_pullhistory(monkeypatch, tmp_path) -> None:
    """Redirect pullhistory.HISTORY_DIR to a per-test path.

    `pullhistory.append` writes to `~/.config/mind-meld/pull-history.jsonl`
    by default. Tests that drive the CLI (test_track_*, test_integration)
    end up calling `_pull_core` / `_push_core`, which call
    `pullhistory.append` with the test's fixture device id (e.g. "dev-a",
    "peerA"). Without this autouse, the lines leak into the user's real
    pull-history file — observed as ~1MB of fixture-named entries
    accumulating from CI / local pytest runs.

    `pullhistory.history_path()` reads `HISTORY_DIR` at call time, so a
    single setattr suffices. Per-test overrides via explicit monkeypatch
    in test bodies still work — last write wins.
    """
    from mind_meld import pullhistory as _pullhistory

    monkeypatch.setattr(_pullhistory, "HISTORY_DIR", tmp_path / "mm_state")


@pytest.fixture(autouse=True)
def _isolate_mm_events_path(request, monkeypatch, tmp_path) -> None:
    """Redirect DEFAULT_SOURCES['mm-events'].path to a per-test directory.

    `mm init` (TestInitFlow + any future runner-driven test) calls
    `_run_events_backfill` -> `get_sources(config)`, which expands the
    mm-events source path. Without this isolation, every successful init
    writes backfilled events to the user's real
    `~/.local/share/mind-meld/events/<random-id>-<date>.jsonl`. Observed
    as 30+ phantom device-id files accumulating from local pytest runs
    after v0.11.8 added the init backfill — those phantom devices then
    inflate retro-fleet's "M of N known machines" header.

    `setitem` mutates the existing dict in place so consumers that did
    `from mind_meld.config import DEFAULT_SOURCES` (cli.py at module
    import time) see the patched path through their existing binding.
    Replacing the list via `setattr` would only update `config`'s
    namespace and leave `cli`'s stale reference to the original list.

    Opt-out: tests that assert on the canonical `~/.local/share/mind-meld`
    default value (e.g. `tests/test_config.py::TestMmEventsSource::
    test_mm_events_default_shape`) can decorate themselves with
    `@pytest.mark.no_mm_events_isolation` to skip the patch.
    """
    if request.node.get_closest_marker("no_mm_events_isolation"):
        return
    from mind_meld import config as _config

    target = next(
        (s for s in _config.DEFAULT_SOURCES if s.get("name") == "mm-events"),
        None,
    )
    if target is None:
        return
    monkeypatch.setitem(target, "path", str(tmp_path / "_isolated_mm_events"))


@pytest.fixture(autouse=True)
def _isolate_retros_dir(monkeypatch, tmp_path) -> None:
    """Redirect retro snapshots to a per-test directory.

    ``aggregator.main()`` saves a JSON snapshot to
    ``~/.local/share/mind-meld/retros/`` after every successful run for
    trend deltas. Without isolation, every test invoking ``main()`` would
    pollute the user's real retros dir AND read whatever was previously
    cached there — non-deterministic. Same pattern as
    ``_isolate_identity_cache`` / ``_isolate_pullhistory``.

    Uses the ``MM_RETROS_DIR`` env hook the aggregator already provides
    (mirroring ``MM_EVENTS_DIR``) — no monkeypatch into module internals
    needed.
    """
    monkeypatch.setenv("MM_RETROS_DIR", str(tmp_path / "_isolated_retros"))


def pytest_configure(config) -> None:
    """Register the `no_mm_events_isolation` marker so pytest doesn't
    warn about unknown markers."""
    config.addinivalue_line(
        "markers",
        "no_mm_events_isolation: opt out of the autouse "
        "_isolate_mm_events_path fixture (for tests that assert on "
        "DEFAULT_SOURCES['mm-events'].path canonical value)",
    )


@pytest.fixture
def test_root_salt() -> bytes:
    """Exported for tests that need the fixture's root_salt explicitly."""
    return _TEST_ROOT_SALT


@pytest.fixture
def test_memory_kb() -> int:
    """Exported for tests that need the fixture's memory_kb explicitly."""
    return _TEST_MEMORY_KB


# ─── Shared CLI integration helpers ──────────────────────────────────────
#
# Plain module-level functions imported by tests that drive the CLI via
# CliRunner. Canonical home for these so multiple test modules can pull from
# a single location.

PASSPHRASE = "shared-cli-test-passphrase"
MEMORY_KB = 1024


def _make_config(tmp_path, storage_dir, claude_dir, device_id="dev-a", device_name="Mac A"):
    """Write a valid config.toml for a single-claude-source setup."""
    from mind_meld.config import save_config

    config_path = tmp_path / "config.toml"
    config = {
        "device": {"id": device_id, "name": device_name},
        "storage": {"path": str(storage_dir)},
        "sync": {
            "max_file_size": 52_428_800,
            "sources": [
                {"name": "claude", "path": str(claude_dir), "type": "claude"},
            ],
        },
        "crypto": {"argon2_memory_kb": MEMORY_KB},
    }
    save_config(config, config_path)
    return config_path, config


def _populate_claude(claude_dir, contents: str = "Data scientist") -> None:
    """Populate a claude_dir with a single memory file so push has work to do."""
    memory = claude_dir / "projects" / "-Users-kb-myapp" / "memory"
    memory.mkdir(parents=True)
    (memory / "role.md").write_text(contents)


def _redirect_sidecar(monkeypatch, tmp_path):
    """Redirect SIDECAR_DIR to a tmp path so log writes are hermetic.

    `_auto_log_path` reads `sidecar.SIDECAR_DIR` at call time, so patching
    the one constant is sufficient — no `mind_meld.cli._AUTO_LOG_DIR` alias
    to keep in sync.
    """
    iso = tmp_path / "sidecar"
    iso.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("mind_meld.sidecar.SIDECAR_DIR", iso)
    return iso


def _redirect_lock(monkeypatch, tmp_path):
    lp = tmp_path / "test.lock"
    monkeypatch.setattr("mind_meld.config.LOCK_PATH", lp)
    monkeypatch.setattr("mind_meld.lockfile.LOCK_PATH", lp)
    return lp


def _setup_real_config(tmp_path, monkeypatch):
    """Full working config so we reach _pull_core / _push_core."""
    from mind_meld.crypto import bootstrap_crypto_init
    from mind_meld.devices import register_device
    from mind_meld.storage.local import LocalBackend

    storage_dir = tmp_path / "storage"
    claude_dir = tmp_path / ".claude"
    _populate_claude(claude_dir)
    config_path, _ = _make_config(tmp_path, storage_dir, claude_dir)

    backend = LocalBackend(storage_dir)
    bootstrap_crypto_init(backend, PASSPHRASE, argon2_memory_kb=MEMORY_KB)
    register_device(backend, "dev-a", "Mac A")

    monkeypatch.setattr("mind_meld.config.CONFIG_PATH", config_path)
    _redirect_lock(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDMELD_PASSPHRASE", PASSPHRASE)
    return claude_dir
