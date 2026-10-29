"""`mm init`'s body and its init-only helpers, independent of command registration.

Raise surface for direct callers of `init`:
- returns after "Mind Meld initialized" on success.
- typer.Exit() (code 0): the user declined to overwrite an existing config or
  declined the orphan-case confirm; nothing local or remote changed.
- typer.Exit(1): a core._error refusal (corrupt or newer mm-crypto-init, wrong or
  mismatched passphrase, lost bootstrap race, no sync sources enabled) or the
  BRICK abort; each prints its own remedy. Rerun `mm init` after following it.
- typer.Abort (exit 1): stdin closed during a prompt.
The `@app.command()` shell and the `mm init --help` text stay in cli.py; this
module never imports cli.
"""

from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from mind_meld import __version__, core, events_tail, fsutil, skill_link, upgrade
from mind_meld import config as _config_module
from mind_meld.config import (
    DEFAULT_ARGON2_MEMORY_KB,
    DEFAULT_MAX_FILE_SIZE,
    DEFAULT_SOURCES,
    DEFAULT_STORAGE_PATH,
    MM_INTERNAL_SOURCE_NAMES,
    get_default_source,
    get_sources,
    load_config,
    save_config,
)
from mind_meld.consoles import console, stderr_console
from mind_meld.crypto import (
    CryptoInitFetch,
    bootstrap_crypto_init,
    fetch_crypto_init,
    load_master_key,
    root_salt_fingerprint,
    set_crypto_session,
    store_passphrase_in_keyring,
    verify_passphrase,
)
from mind_meld.devices import generate_unique_short_device_id, list_devices, register_device
from mind_meld.errors import NEWER_STORAGE_URL, CryptoError, MindMeldError, StorageError
from mind_meld.safety import safe_str
from mind_meld.storage.keys import DATA_PREFIX, DEVICES_PREFIX, MANIFESTS_PREFIX, device_key
from mind_meld.storage.local import LocalBackend


@dataclass
class _StorageOccupancy:
    """Authoritative occupancy signals for the init guard.

    `devices/` entries alone are NOT a trustworthy signal \u2014 list_devices
    silently drops malformed entries, so an attacker or corruption event
    could leave `devices/` empty while `data/` and `manifests/` still
    hold load-bearing encrypted state. The init guard checks the stronger
    signals (mm-crypto-init + blobs + manifests) first.
    """

    has_crypto_init: bool  # fetch_crypto_init().status == "ok"
    has_corrupt_crypto_init: bool
    has_any_blobs: bool  # any data/**/*.enc
    has_any_manifests: bool  # any manifests/**/*.enc
    has_any_devices: bool  # devices/ non-empty (weakest signal)


def _probe_storage_occupancy(backend: LocalBackend) -> _StorageOccupancy:
    """Check which kinds of state a storage root already holds.

    Called by `init` before prompting for a passphrase. Each probe is
    cheap (list_keys with a prefix) and degrades to False on any error,
    so a racy backend never prevents init from continuing.
    """
    fetch = fetch_crypto_init(backend)

    def _any_enc(prefix: str) -> bool:
        try:
            for k in backend.list_keys(prefix):
                if k.endswith(".enc"):
                    return True
        except Exception:
            return False
        return False

    def _has_devices() -> bool:
        try:
            for _ in backend.list_keys(DEVICES_PREFIX):
                return True
        except Exception:
            return False
        return False

    return _StorageOccupancy(
        has_crypto_init=(fetch.status == "ok"),
        has_corrupt_crypto_init=(fetch.status == "corrupt"),
        has_any_blobs=_any_enc(DATA_PREFIX),
        has_any_manifests=_any_enc(MANIFESTS_PREFIX),
        has_any_devices=_has_devices(),
    )


def _init_storage_guard(
    occupancy: _StorageOccupancy,
    existing_device_id: str | None,
    existing_device_name: str | None,
) -> None:
    """Gate init when storage already holds state. Exits nonzero on refuse.

    Two tiers, priority-ordered by severity:

      * BRICK case \u2014 mm-crypto-init is MISSING but blobs or manifests
        exist. About to rebootstrap a new root_salt, which makes every
        existing blob unrecoverable. Refuse by default; require exact
        typed "BRICK" (case-sensitive) to proceed. Non-TTY aborts via
        typer.prompt.

      * ORPHAN case \u2014 mm-crypto-init is ok AND any other occupancy
        signal is true. A new device_id gets minted; old blobs remain
        readable under the shared root_salt but the old device entry
        no longer pushes or pulls. Warn + typer.confirm. Non-TTY aborts.
    """
    # BRICK first: most severe.
    if not occupancy.has_crypto_init and (occupancy.has_any_blobs or occupancy.has_any_manifests):
        stderr_console.print(
            "[red]DANGER:[/red] mm-crypto-init is missing from storage, but "
            "encrypted blobs/manifests still exist. Initializing now generates "
            "a NEW root_salt \u2014 [bold]every existing blob becomes "
            "unrecoverable[/bold]. If another device still has a working "
            "mm-crypto-init in its iCloud cache, wait for sync to reconcile "
            "and retry init instead."
        )
        typed = typer.prompt('Type "BRICK" (case-sensitive) to confirm and proceed')
        if typed != "BRICK":
            stderr_console.print("[yellow]Aborted.[/yellow] No state changed.")
            raise typer.Exit(1)
        return

    # Orphan case: storage has state and we're about to add a device entry.
    any_storage = (
        occupancy.has_any_blobs or occupancy.has_any_manifests or occupancy.has_any_devices
    )
    if occupancy.has_crypto_init and any_storage:
        if existing_device_id:
            msg = (
                f"This creates a new device entry, orphaning existing device "
                f"'{existing_device_id}' ({existing_device_name or 'unknown'}). "
                f"Old blobs remain readable under the shared root_salt but the "
                f"old device entry will no longer push or pull. Proceed?"
            )
        else:
            msg = (
                "Storage already holds encrypted state. A new device_id will "
                "be minted and registered alongside the existing devices. "
                "Proceed?"
            )
        if not typer.confirm(msg, default=False):
            raise typer.Exit()
        return

    # Not gated: first-device path on empty storage, or mm-crypto-init
    # is ok and nothing else exists.


def _prompt_sources() -> list[dict[str, Any]]:
    """Prompt for each known source type; return the enabled entries.

    User-facing sources (claude, gstack) become a Y/n prompt via
    `_prompt_source_toggle`. Default is Y when the qualifying source tree
    exists on disk, N otherwise; Muse always defaults to N.
    This nudges users toward only-enabling-what-they-have without making it
    impossible to enable a source whose directory doesn't exist yet (e.g. new
    machine, same project about to be cloned).

    mm-internal sources (mm-events, Group 7+) auto-include without
    prompting — they're mm-owned infrastructure for fleet-wide features
    (retro-fleet) and shouldn't burden the init UX with a question whose
    only legitimate answer is "yes." Per-machine opt-out is via
    `mm disable-source mm-events` post-init (v0.10.0).

    Source paths stay in tilde-form in the returned dicts so they round-
    trip through TOML readably. `get_sources()` expands at use time.
    """
    enabled: list[dict[str, Any]] = []
    for default in DEFAULT_SOURCES:
        if default["name"] in MM_INTERNAL_SOURCE_NAMES:
            src = get_default_source(default["name"])
            if src is not None:
                enabled.append(src)
            continue
        exists = core._source_path_is_detected(default)
        # Muse is opt-in even when detected: its memory/ tree is host-created,
        # and enabling the source also consents to the usage reader.
        default_on = exists and default["name"] != "muse"
        if core._prompt_source_toggle(default, current_state=default_on, detected=exists):
            src = get_default_source(default["name"])
            if src is not None:
                enabled.append(src)
    return enabled


def _load_prior_device_metadata() -> tuple[str | None, str | None]:
    """Return (id, name) from any existing config, best-effort.

    Used to name the device that would be orphaned by re-init. Malformed
    or missing config returns (None, None) — the orphan warning just
    loses the descriptive name.
    """
    if not _config_module.CONFIG_PATH.exists():
        return None, None
    try:
        prior = load_config()
    except MindMeldError:
        return None, None
    return prior.get("device", {}).get("id"), prior.get("device", {}).get("name")


def _prompt_passphrase(is_first_device: bool) -> str:
    """Prompt for the encryption passphrase. Double-prompt on first-device.

    Exits via _error on empty input or (first-device) mismatch. Returns
    the validated passphrase string — caller uses it for bootstrap or
    verify but MUST NOT commit to keyring until crypto validation passes.
    """
    passphrase = typer.prompt("Encryption passphrase", hide_input=True)
    if not passphrase:
        core._error("Passphrase cannot be empty.")
    if is_first_device:
        confirm = typer.prompt("Confirm passphrase", hide_input=True)
        if passphrase != confirm:
            core._error("Passphrases don't match.")
    return passphrase


def _bootstrap_or_verify_crypto(
    backend: LocalBackend,
    passphrase: str,
    is_first_device: bool,
    fetch: CryptoInitFetch,
) -> tuple[bytes, int, bytes]:
    """Return (root_salt, argon2_memory_kb, keycheck_blob).

    First-device path: try to bootstrap. On StorageError (lost race), fall
    through to verify against the winner's mm-crypto-init — this is the
    same flow a second device would take.

    Second-device path: verify the passphrase against the existing
    mm-crypto-init. Failure aborts via _error; no local state is written.

    Sets the crypto session as a side effect so downstream calls can
    derive blob-level keys.
    """
    if is_first_device:
        arrived = fetch_crypto_init(backend)
        if arrived.newer_version is not None:
            core._error(core._newer_format_message(arrived.newer_version))
        try:
            bootstrap = bootstrap_crypto_init(
                backend, passphrase, argon2_memory_kb=DEFAULT_ARGON2_MEMORY_KB
            )
        except StorageError:
            # Race: another device wrote mm-crypto-init between our fetch and
            # our put. Fall through to second-device verify with the winner's blob.
            console.print(
                "  [dim]Another device bootstrapped concurrently — "
                "verifying against their mm-crypto-init.[/dim]"
            )
            retry_fetch = fetch_crypto_init(backend)
            if retry_fetch.newer_version is not None:
                core._error(core._newer_format_message(retry_fetch.newer_version))
            if retry_fetch.status != "ok":
                core._error("init: lost bootstrap race but peer's mm-crypto-init not ok.")
            assert retry_fetch.root_salt is not None
            assert retry_fetch.argon2_memory_kb is not None
            assert retry_fetch.keycheck_blob is not None
            verified = _verify_existing_crypto_init(
                retry_fetch,
                passphrase,
                success_message="  Verified passphrase against peer mm-crypto-init.",
            )
            try:
                core._apply_verified_crypto_repair(backend, retry_fetch)
            except MindMeldError as e:
                core._error(str(e))
            return verified

        arrived = fetch_crypto_init(backend)
        if arrived.newer_version is not None:
            core._error(core._newer_format_message(arrived.newer_version))
        assert bootstrap.root_salt is not None
        assert bootstrap.argon2_memory_kb is not None
        assert bootstrap.keycheck_blob is not None
        root_salt = bootstrap.root_salt
        argon2_memory_kb = bootstrap.argon2_memory_kb
        keycheck_blob = bootstrap.keycheck_blob
        set_crypto_session(root_salt, argon2_memory_kb)
        console.print(
            f"  mm-crypto-init bootstrapped (root_salt fp={root_salt_fingerprint(root_salt)})."
        )
        return root_salt, argon2_memory_kb, keycheck_blob

    # Second-device: verify against the fetch we already did.
    verified = _verify_existing_crypto_init(
        fetch,
        passphrase,
        success_message=(
            "  Verified passphrase against existing mm-crypto-init "
            f"(root_salt fp={root_salt_fingerprint(fetch.root_salt)})."
        ),
    )

    try:
        core._apply_verified_crypto_repair(backend, fetch)
    except MindMeldError as e:
        core._error(str(e))
    return verified


def _verify_existing_crypto_init(
    fetch: CryptoInitFetch,
    passphrase: str,
    *,
    success_message: str,
) -> tuple[bytes, int, bytes]:
    """Verify an already-fetched crypto init and activate its session.

    The caller owns any race retry before invoking this helper. In particular,
    `_bootstrap_or_verify_crypto` must never feed its stale pre-bootstrap fetch
    here after an exclusive-create loss.
    """
    assert fetch.root_salt is not None
    assert fetch.argon2_memory_kb is not None
    assert fetch.keycheck_blob is not None
    root_salt = fetch.root_salt
    argon2_memory_kb = fetch.argon2_memory_kb
    keycheck_blob = fetch.keycheck_blob
    set_crypto_session(root_salt, argon2_memory_kb)
    master_key = load_master_key(passphrase, root_salt, argon2_memory_kb)
    try:
        verify_passphrase(master_key, keycheck_blob)
    except CryptoError as e:
        core._error(str(e))
    console.print(success_message)
    return root_salt, argon2_memory_kb, keycheck_blob


def _register_and_save(
    config: dict,
    backend: LocalBackend,
    device_id: str,
    device_name: str,
    passphrase: str,
) -> None:
    """Register the device, persist config, and store the passphrase.

    Order matters: device register (storage write) → config write (local
    pointer) → keyring store. The "remote first, local pointer last"
    pattern is the canonical filesystem/DB transaction discipline — a
    crash before the local pointer is committed leaves the remote with
    an inert breadcrumb (recoverable on retry), never the inverse half-
    state where local claims a device storage doesn't recognize. A typo'd
    passphrase on the second-device path is caught in crypto validation
    BEFORE this function runs, so keyring never holds an invalid secret.

    Crash safety (Track 5D, v0.9.4):
      * register fails → re-raise; nothing local was written, retry init
        runs the first-device path naturally.
      * save_config fails → best-effort `backend.delete(devices/<id>.json)`
        before re-raising. Normal save failures (disk full, permissions)
        self-clean so retry init doesn't trip `_init_storage_guard`'s
        orphan-case warning. If the cleanup itself fails, the original
        save error propagates — masking the real cause behind a confusing
        secondary error is worse than the cosmetic orphan that remains.
      * SIGKILL between register and save_config → cosmetic orphan device
        entry in storage. `_init_storage_guard` warns + prompts on retry
        init; user confirms and a fresh device_id is minted alongside.
        The orphan is inert (no `last_seen`, never used).
      * Pre-existing victims of the v0.8.15..v0.9.3 inverse half-state
        (config has device_id, storage lacks devices/<id>.json) self-heal
        on first push via `_ensure_device_registered` at `_push_core`
        entry — see Track 5D Task 2b.
    """
    # Precompute the storage key so the cleanup-warning f-string below
    # cannot itself raise from `device_key(...)` validation and mask the
    # original save_config error (codex adversarial 2026-04-25).
    dev_key = device_key(device_id)
    register_device(backend, device_id, device_name)
    try:
        save_config(config)
    except Exception:
        # Best-effort cleanup: keep the device entry from accumulating in
        # storage when save_config raises normally (disk full, permissions,
        # bad config path). Without this, repeated init failures mint a new
        # orphan storage entry per attempt and trip `_init_storage_guard`'s
        # orphan-case warning on retry. SIGKILL still leaves the cosmetic
        # orphan — that's the rare event we accept structurally via the
        # order swap.
        try:
            backend.delete(dev_key)
        except Exception as cleanup_exc:
            # Per the visible-failure contract (docs/invariants/sync.md): load-bearing warnings
            # signal data-at-risk degradation and must reach stderr even in
            # quiet mode. Original save error wins via bare `raise` below —
            # surfacing the cleanup failure but not letting it mask the
            # real cause.
            stderr_console.print(
                f"mm: warning: cleanup of {dev_key} after "
                f"save_config failure failed ({type(cleanup_exc).__name__}): "
                f"{cleanup_exc}"
            )
        raise
    console.print(f"  Device registered: {device_name} ({device_id})")
    console.print(f"  Config written to {_config_module.CONFIG_PATH}")

    # store_passphrase_in_keyring narrowed its own catch to
    # (KeyringError, ImportError) in v0.8.9. Init has already committed
    # config + device registration at this point, so aborting on a
    # non-KeyringError kind (e.g., RuntimeError from a broken backend)
    # would leave the user half-initialized with a cryptic traceback.
    # Treat any keyring failure as "not available" + warn; env-var is a
    # valid fallback and the user can re-run init if they want to retry.
    try:
        stored = store_passphrase_in_keyring(passphrase)
    except Exception as e:
        console.print(f"  [yellow]Keyring backend error ({type(e).__name__}):[/yellow] {e}")
        stored = False

    if stored:
        console.print("  Passphrase stored in OS keyring.")
    else:
        console.print(
            "  [yellow]No keyring available.[/yellow] "
            "Set MINDMELD_PASSPHRASE environment variable instead."
        )


_ICLOUD_DRIVE_ROOT = Path("~/Library/Mobile Documents/com~apple~CloudDocs").expanduser()


def _auto_pin_storage_for_icloud(storage_path: Path) -> None:
    """Auto-pin an iCloud Drive storage path so future pulls don't block on
    iCloud File Provider materialization.

    Runs ``brctl download <storage_path>`` (Apple's iCloud File Provider CLI,
    /usr/bin/brctl). brctl is non-destructive, idempotent, and async — it
    queues the request and returns immediately while iCloud materializes
    files in the background. On any error (brctl missing, timeout, non-zero
    exit) falls back to a one-line Finder tip.

    Silent for non-iCloud storage paths: the slow-pull case only exists when
    blobs go cold via the iCloud File Provider; a regular local folder
    doesn't need pinning.

    Called once at init success. If iCloud later evicts blobs (storage
    pressure), the user can re-pin manually via the same command. We do NOT
    re-run on every push/pull — that would be invasive overhead for an
    onboarding nudge.
    """
    try:
        is_icloud = storage_path.resolve(strict=False).is_relative_to(_ICLOUD_DRIVE_ROOT)
    except (OSError, ValueError):
        is_icloud = False

    if not is_icloud:
        return

    try:
        result = subprocess.run(
            ["brctl", "download", str(storage_path)],
            check=False,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            console.print(
                "  [dim]Storage pinned for fast pulls "
                "(iCloud will keep blobs resident on this Mac).[/dim]"
            )
            return
    except (
        FileNotFoundError,
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
        subprocess.SubprocessError,
    ):
        pass

    console.print(
        "  [dim]Tip: keep blobs resident for fast pulls. In Finder, "
        f'right-click "{storage_path}" › Keep Downloaded.[/dim]'
    )


def init() -> None:
    # Capture prior device metadata BEFORE any prompt so the orphan-case
    # warning can name the device about to be left behind.
    existing_device_id, existing_device_name = _load_prior_device_metadata()
    if _config_module.CONFIG_PATH.exists():
        overwrite = typer.confirm(
            f"Config already exists at {_config_module.CONFIG_PATH}. Overwrite?"
        )
        if not overwrite:
            raise typer.Exit()

    console.print(f"[bold]Mind Meld v{__version__} — init[/bold]\n")

    # Storage path first: we need a backend to probe mm-crypto-init.
    storage_path = typer.prompt("Storage folder path", default=DEFAULT_STORAGE_PATH)
    full_path = Path(storage_path).expanduser()
    fsutil.refuse_real_state_write_under_pytest(full_path)
    full_path.mkdir(parents=True, exist_ok=True)
    console.print(f"  Storage: {full_path}")

    # Lightweight backend (config is not yet written; instantiate directly).
    backend = LocalBackend(str(full_path))

    # Probe storage for mm-crypto-init BEFORE committing any local state.
    fetch = fetch_crypto_init(backend)
    if fetch.newer_version is not None:
        core._error(core._newer_format_message(fetch.newer_version))
    if fetch.status == "corrupt":
        core._error(
            "init: mm-crypto-init at storage root is corrupt. If another device "
            "still has a valid copy in its local iCloud cache, wait for sync to "
            f"reconcile and retry. For non-destructive recovery, see {NEWER_STORAGE_URL}."
        )

    # Two-tier guard: storage occupancy is authoritative state, not just
    # local config. Runs BEFORE any further prompt, so a refused init never
    # touches the keyring or writes config.
    occupancy = _probe_storage_occupancy(backend)
    _init_storage_guard(
        occupancy,
        existing_device_id=existing_device_id,
        existing_device_name=existing_device_name,
    )

    is_first_device = fetch.status == "missing"

    # Init-time collision prevention: scan existing peers and regenerate
    # the 8-char device_id on collision. UUID4 prefix collisions are
    # extremely unlikely (~1 in 4 billion per draw) but a deterministic-RNG
    # bug or a cloned-from-snapshot peer could collide reproducibly.
    # Cheap to defend in depth; runtime lookup_device_by_short_id still
    # handles a stray collision if we exhaust the retry budget.
    existing_devices = list_devices(backend) if not is_first_device else []
    device_id = generate_unique_short_device_id(existing_devices)
    device_name = typer.prompt("Device name", default=_default_device_name())

    passphrase = _prompt_passphrase(is_first_device)
    root_salt, argon2_memory_kb, _keycheck_blob = _bootstrap_or_verify_crypto(
        backend, passphrase, is_first_device, fetch
    )

    # All crypto passed; it's safe to prompt for sources and write state.
    # Refuse if no sources enabled — a config with zero sources leaves
    # push/pull silently no-op'ing.
    #
    # Ordering note: source prompt runs AFTER crypto bootstrap to keep the
    # second-device wrong-passphrase path fast — we want "wrong pw" to
    # surface BEFORE we ask about sources, not after. Consequence: on a
    # FIRST-device refuse-all, mm-crypto-init is already written to storage
    # and orphaned. This is benign: re-running init hits the second-device
    # path (mm-crypto-init now exists), user's same passphrase verifies
    # against their own earlier bootstrap, and the second attempt finishes
    # cleanly. No data loss, just a storage-side breadcrumb. See
    # TestInitFlow::test_first_device_refuse_all_is_recoverable.
    sources = _prompt_sources()
    # mm-internal sources (mm-events) auto-include and don't count toward
    # the user-intent guard — a config with only mm-internal sources is
    # effectively "user wanted nothing synced," same as the pre-Group-7
    # zero-sources case. Push/pull would silently no-op for the user's
    # own data; better to refuse and let them re-run.
    user_facing_sources = [s for s in sources if s["name"] not in MM_INTERNAL_SOURCE_NAMES]
    if not user_facing_sources:
        core._error(
            "init: no sync sources enabled. Re-run 'mm init' and accept at least one source."
        )

    config: dict = {
        "device": {"id": device_id, "name": device_name},
        "storage": {"path": storage_path},
        "sync": {
            "sources": sources,
            "max_file_size": DEFAULT_MAX_FILE_SIZE,
        },
        "crypto": {
            "argon2_memory_kb": argon2_memory_kb,
            "root_salt_fp": root_salt_fingerprint(root_salt),
        },
    }

    _register_and_save(config, backend, device_id, device_name, passphrase)

    # Seam 1 — transition detection (init path). Seeds the
    # last_seen_self_version cache so any subsequent upgrade transition is
    # logged correctly. Routed here directly rather than through
    # _get_config / _auto_command_setup because init has its own first-run
    # path with no prior config to load (D6 reasoning).
    upgrade.run_transition_hook(config)

    # Group 8 / Track 8A: drop the retro-fleet skill symlink at init time
    # (no 24h gate here — first-install pass should always try). Idempotent
    # if the user already has a correct symlink. Conflicts emit a one-line
    # notice; failures are forensic-only.
    # Track 25C: resolve sources BEFORE the installer so consent is known.
    # Hook position relative to _register_and_save and _run_events_backfill
    # is unchanged. Init explicitly creates the default root before backfill.
    resolved_sources = get_sources(config, bootstrap=True)
    may_create = skill_link.consented_agent_keys(config, resolved_sources)
    try:
        skill_link._ensure_retro_skill_links(dry_run=False, explicit=True, may_create=may_create)
    except Exception as e:
        stderr_console.print(
            f"mm: notice: retro-fleet skill installation failed: {type(e).__name__}: {safe_str(e)}"
        )

    # Init-time event backfill (v0.11.8). Captures the past 30 days of git
    # commits + a full sessions inventory so retro-fleet works immediately
    # after init, without waiting for the first push to populate events.
    # Sources were explicitly bootstrapped above; the backfill creates events/
    # when appending rows. Forensic-only on failure; init proceeds.
    events_tail._run_events_backfill(config, resolved_sources, device_id)

    console.print("\n[green]Mind Meld initialized. Run 'mm push' to sync.[/green]")

    # Track 9A: auto-pin iCloud storage so the user's first pull (and every
    # subsequent pull) reads resident blobs instead of blocking on iCloud
    # File Provider materialization. Best-effort; falls back to a Finder
    # right-click tip on any error. Silent for non-iCloud storage paths.
    _auto_pin_storage_for_icloud(full_path)


def _default_device_name() -> str:
    """Generate a default device name from hostname."""
    return socket.gethostname()
