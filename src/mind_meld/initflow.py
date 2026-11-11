"""`mm init`'s body and its init-only helpers, independent of command registration.

Two flows. Plain `mm init` joins a fleet: it needs the fleet's key header
(mm-crypto-init) in the storage folder, verifies the passphrase against it and
registers this device. `mm init --new-fleet` is the only path that creates the
key header, and only in an empty folder. When a managed join accepts the folder
offer (or the folder is already shared with a peer) and the header has not
arrived yet, `init` waits up to `HEADER_WAIT_SECONDS` (`HEADER_POLL_SECONDS` per
step); a plain init that still finds no header stops without writing anything.

Raise surface for direct callers of `init`:
- returns the verified passphrase after a join, so the `cli.init` shell can
  pull with it, and None after `--new-fleet`.
- typer.Exit() (code 0): the user declined to overwrite an existing config or
  declined the re-init confirm; nothing local or remote changed.
- typer.Exit(1): a core._error refusal (no key header yet, Ctrl-C while waiting
  for the key header, a folder that `--new-fleet` will not accept, corrupt or
  newer mm-crypto-init, wrong passphrase, a header that arrived during the
  prompts, no sync sources enabled); each prints its own remedy.
- typer.Abort (exit 1): stdin closed during a prompt; the shell turns it into
  a "needs a terminal" error when stdin is not a terminal.
The `@app.command()` shell and the `mm init --help` text stay in cli.py; this
module never imports cli.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from mind_meld import (
    __version__,
    attemptlog,
    core,
    events_tail,
    fsutil,
    skill_link,
    syncthing,
    upgrade,
)
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
from mind_meld.errors import (
    KEY_HEADER_COPIES_URL,
    NEWER_STORAGE_URL,
    ConfigError,
    CryptoError,
    MindMeldError,
    StorageError,
    os_error_cause,
)
from mind_meld.safety import safe_str, safe_terminal_str
from mind_meld.storage.keys import DATA_PREFIX, DEVICES_PREFIX, MANIFESTS_PREFIX, device_key
from mind_meld.storage.local import LocalBackend

# The temp name fsutil.atomic_write_bytes and LocalBackend.put_exclusive create
# (`tempfile.mkstemp(suffix=".tmp")`). `(?d)` lets Syncthing delete a folder
# that holds nothing else. `.stignore` itself never syncs.
STIGNORE_PATTERN = "tmp*.tmp"
STIGNORE_LINE = f"(?d){STIGNORE_PATTERN}"

# Top-level names a folder may hold and still be "empty" for `--new-fleet`.
_BOOKKEEPING_NAMES = frozenset({".stfolder", ".stignore", ".stversions", ".DS_Store"})
_FLEET_DIRS = frozenset({"data", "manifests", "devices"})
_IN_FLIGHT_PREFIXES = (".syncthing.", "~syncthing~")
_HEADER_STEM = "mm-crypto-init"
# Bounded wait after a managed join accepts the folder offer (or finds the
# folder already shared with a peer) before Syncthing has delivered the header
# file. Named without forbidden credential words (AGENTS.md).
HEADER_WAIT_SECONDS = 120
HEADER_POLL_SECONDS = 1


@dataclass(frozen=True)
class _SyncthingSetup:
    """Syncthing management decision for one `mm init` run."""

    record: syncthing.ManagedRecord | None
    local_id: str | None
    persisted: bool


def _check_syncthing() -> _SyncthingSetup:
    """Read-only Syncthing configuration check before `init` writes anything."""
    try:
        recorded_id = _config_module.load_syncthing_record()
    except ConfigError as e:
        core._error(
            f"{safe_terminal_str(e)} Nothing was written. Fix or remove that file, "
            "then run 'mm init' again."
        )
    check = syncthing.classify_config(sanitize=safe_terminal_str)
    if check.reach in ("not-found", "unreachable"):
        core._error(
            "init: Syncthing is not running on this machine (or its API at the GUI "
            "address is not answering), so mm cannot check its configuration. Nothing "
            "was written. Start it, then run 'mm init' again. "
            f"Run: {core._syncthing_start_command()}"
        )
    if (
        check.reach == "ok"
        and check.source == "rest"
        and recorded_id is not None
        and recorded_id == check.local_device_id
    ):
        return _SyncthingSetup(
            record=syncthing.ManagedRecord(device_id=recorded_id),
            local_id=check.local_device_id,
            persisted=True,
        )
    if check.reach == "ok" and check.source == "rest" and check.state == "default":
        record = check.managed_record()
        return _SyncthingSetup(
            record=record,
            local_id=record.device_id,
            persisted=False,
        )
    if (check.reach == "ok" and check.state == "pre-existing") or (
        check.reach == "address-unsupported" and check.state == "pre-existing"
    ):
        console.print(
            "  Syncthing on this machine already has its own configuration, so mm "
            f'changes nothing in it. Set it up as README "{core.SYNCTHING_GUIDE_HEADING}" '
            "describes."
        )
        return _SyncthingSetup(
            record=None,
            local_id=check.local_device_id,
            persisted=False,
        )
    reach_s = safe_terminal_str(check.reach)
    cause_s = safe_terminal_str(check.cause or check.state or "unknown")
    core._error(
        f"init: could not read Syncthing's configuration ({reach_s}/{cause_s}), "
        "so mm does not know whether it may set Syncthing up. Nothing was written. "
        "Run: mm diag"
    )


def _save_syncthing_record_or_stop(setup: _SyncthingSetup) -> None:
    """Persist the managed Syncthing record when not already persisted."""
    assert setup.record is not None
    if setup.persisted:
        return
    try:
        _config_module.save_syncthing_record(setup.record.device_id)
    except (OSError, StorageError, ConfigError) as e:
        cause = (
            os_error_cause(e) if isinstance(e, (OSError, StorageError)) else safe_terminal_str(e)
        )
        core._error(
            f"init: could not record that mm manages Syncthing here ({cause}). "
            "Nothing else was written. Fix that, then run 'mm init' again."
        )


def _require_syncthing(
    result: syncthing.WriteResult,
    what: str,
    *,
    new_fleet: bool = False,
) -> None:
    """Stop with a targeted remedy when a Syncthing write did not succeed."""
    if result.outcome in ("ok", "unchanged"):
        return
    rerun_cmd = "mm init --new-fleet" if new_fleet else "mm init"
    if result.outcome in ("unreachable", "not-found"):
        core._error(
            "init: Syncthing stopped answering while mm was setting it up "
            f"({safe_terminal_str(what)}). A rerun continues from where it stopped. "
            f"Run: {core._syncthing_start_command()}"
        )
    if result.outcome == "refused" and result.cause == "default-folder-in-use":
        core._error(
            "init: Syncthing's Default Folder at ~/Sync holds files or is shared, "
            "and mm's folder would sit inside it. Earlier setup changes remain. "
            "A rerun continues from where it stopped. "
            f"Choose a storage path outside ~/Sync, then run '{rerun_cmd}' again."
        )
    if result.outcome == "refused" and result.cause == "folder-exists":
        core._error(
            "init: Syncthing already has a mind-meld folder with a different setup. "
            "Check its configured path, required options and shared devices in "
            f'Syncthing\'s GUI (README "{core.SYNCTHING_GUIDE_HEADING}"), then use that path. '
            f"Run: {rerun_cmd}"
        )
    if result.outcome == "refused" and result.cause == "path-overlap":
        core._error(
            "init: the chosen path overlaps another configured Syncthing folder. "
            "Check the folder paths in Syncthing's GUI and choose a non-overlapping path. "
            f"Run: {rerun_cmd}"
        )
    out_s = safe_terminal_str(result.outcome)
    cause_s = safe_terminal_str(result.cause or "unknown")
    core._error(
        f"init: Syncthing did not {safe_terminal_str(what)} ({out_s}/{cause_s}). "
        "A rerun continues from where it stopped. "
        "Run: mm diag"
    )


def _is_inside_stock_default_folder(full_path: Path) -> bool:
    """Return True when ``full_path`` is at or inside Syncthing's stock Default Folder."""
    target = full_path.expanduser().resolve()
    stock = syncthing.stock_default_folder_path().expanduser().resolve()
    return target == stock or target.is_relative_to(stock)


def _read_syncthing_state_or_stop(full_path: Path) -> syncthing.SyncthingState:
    """Do not mistake an unreadable setup for an unconfigured folder."""
    state = syncthing.read_state(full_path)
    if state.state != "ok" or (state.folder_path is None and "no-folder" not in state.problems):
        core._error(
            "init: could not check Syncthing's folder setup "
            f"({safe_terminal_str(state.state)}/{safe_terminal_str(state.cause or 'unknown')}). "
            "Nothing was written. Keep the same storage path and check Syncthing. Run: mm diag"
        )
    return state


def _managed_new_fleet_folder(setup: _SyncthingSetup, full_path: Path) -> None:
    """Create mm's Syncthing folder on the managed `--new-fleet` path."""
    assert setup.record is not None
    state = _read_syncthing_state_or_stop(full_path)
    if state.folder_path == full_path.expanduser().resolve() and state.total_peers != 0:
        core._error(
            "init: this Syncthing folder is already shared, or its peer membership "
            "could not be verified. Creating a new fleet here could replace the "
            "existing fleet's key header. Nothing was written. Wait for Syncthing "
            "to deliver the key header, then run 'mm init' without --new-fleet."
        )
    _save_syncthing_record_or_stop(setup)
    writer = syncthing.SyncthingWriter(setup.record)
    if _is_inside_stock_default_folder(full_path):
        _require_syncthing(
            writer.remove_default_folder(),
            "remove the Default Folder",
            new_fleet=True,
        )
    _require_syncthing(
        writer.create_folder(
            _config_module.SYNCTHING_FOLDER_ID,
            _config_module.SYNCTHING_FOLDER_LABEL,
            full_path,
        ),
        "create mm's folder",
        new_fleet=True,
    )


def _read_fleet_device_id(local_id: str | None) -> str:
    """Prompt for and validate a fleet peer's Syncthing device ID before any write."""
    raw = typer.prompt("Syncthing device ID of a machine already in the fleet")
    trimmed = raw.strip()
    candidate = core._normalize_device_id(trimmed)
    why = core._device_id_problem(candidate, local_id)
    if why is not None:
        core._error(
            f"init: '{safe_terminal_str(trimmed)}' is not a Syncthing device ID ({why}). "
            "Nothing was written. Copy the ID from the fleet machine "
            "(Syncthing's Actions > Show ID, or the ID 'mm init --new-fleet' printed there), "
            "then run 'mm init' again."
        )
    return candidate


def _managed_join(setup: _SyncthingSetup, full_path: Path) -> bool | None:
    """Accept the fleet folder and report whether it has peers, when known."""
    assert setup.record is not None
    resolved_target = full_path.expanduser().resolve()
    st = _read_syncthing_state_or_stop(full_path)
    if (
        st.state == "ok"
        and st.folder_id == _config_module.SYNCTHING_FOLDER_ID
        and st.folder_path == resolved_target
    ):
        if any(
            not syncthing.option_is_ok(name, st.options.get(name))
            for name, _, _ in syncthing.FOLDER_OPTIONS
        ):
            _require_syncthing(
                syncthing.WriteResult("refused", "folder-exists"), "reuse the fleet folder"
            )
        return st.total_peers > 0 if st.total_peers is not None else None
    fleet_id = _read_fleet_device_id(setup.local_id)
    # Accepting the offer starts bidirectional sync immediately. A new target
    # must not publish unrelated plaintext or combine two fleets before the
    # arriving header can be verified. Already-configured reruns return above.
    folder = _classify_folder(full_path)
    if folder.error is not None:
        _unreadable_folder(full_path, folder.error)
    if not folder.accepts_new_fleet:
        core._error(
            f"init: cannot share {safe_terminal_str(full_path)} with the fleet because "
            f"it already contains {_entry_names(folder.blockers)}. Nothing was written. "
            "Choose an empty storage folder, then run 'mm init' again."
        )
    _save_syncthing_record_or_stop(setup)
    writer = syncthing.SyncthingWriter(setup.record)
    _require_syncthing(writer.add_device(fleet_id), "add the fleet device")
    _require_syncthing(writer.mark_introducer(fleet_id), "mark the fleet device as introducer")
    local_id = setup.local_id or setup.record.device_id
    console.print(f"  This machine's Syncthing device ID: {safe_str(local_id)}")
    if _is_inside_stock_default_folder(full_path):
        _require_syncthing(writer.remove_default_folder(), "remove the Default Folder")
    offer_res = writer.accept_folder_offer(
        _config_module.SYNCTHING_FOLDER_ID,
        fleet_id,
        _config_module.SYNCTHING_FOLDER_LABEL,
        full_path,
    )
    if offer_res.outcome == "refused" and offer_res.cause == "no-offer":
        core._error(
            "init: the fleet machine has not shared mm's folder "
            f"('{_config_module.SYNCTHING_FOLDER_ID}') with this machine yet. "
            "No device entry, config or key header was written. On the fleet "
            "machine, approve this machine with "
            f"'mm devices approve {safe_terminal_str(local_id)}' "
            f'(or by hand, README "{core.SYNCTHING_GUIDE_HEADING}"), wait for '
            "Syncthing, then run 'mm init' here again."
        )
    _require_syncthing(offer_res, "accept the fleet folder offer")
    return True


@dataclass(frozen=True)
class _Folder:
    """A storage folder's top-level entries, sorted into what each means for init."""

    exists: bool
    in_flight: tuple[str, ...] = ()  # Syncthing temporary names: delivery in progress
    fleet: tuple[str, ...] = ()  # data/, manifests/, devices/, any key-header spelling
    other: tuple[str, ...] = ()
    error: OSError | None = None

    @property
    def blockers(self) -> tuple[str, ...]:
        return self.in_flight + self.fleet + self.other

    @property
    def accepts_new_fleet(self) -> bool:
        return self.error is None and not self.blockers


def _classify_folder(path: Path) -> _Folder:
    """Sort the folder's top-level entries; never writes."""
    try:
        with os.scandir(path) as entries:
            names = sorted(entry.name for entry in entries)
    except FileNotFoundError:
        return _Folder(exists=False)
    except OSError as e:
        return _Folder(exists=True, error=e)
    in_flight: list[str] = []
    fleet: list[str] = []
    other: list[str] = []
    for name in names:
        if name in _BOOKKEEPING_NAMES:
            continue
        if name.startswith(_IN_FLIGHT_PREFIXES):
            in_flight.append(name)
        elif name in _FLEET_DIRS or name.startswith(_HEADER_STEM):
            fleet.append(name)
        else:
            other.append(name)
    return _Folder(True, tuple(in_flight), tuple(fleet), tuple(other))


def _entry_names(names: tuple[str, ...]) -> str:
    """Up to five peer-controlled names, each as safe single-line text."""
    shown = ", ".join(f"'{safe_terminal_str(name)}'" for name in names[:5])
    return shown + (f" and {len(names) - 5} more" if len(names) > 5 else "")


def _unreadable_folder(path: Path, error: OSError) -> None:
    core._error(
        f"init: cannot read the storage folder {safe_terminal_str(path)}: "
        f"{os_error_cause(error)}. Nothing was written. Fix the path or its "
        "permissions, then run 'mm init' again."
    )


def _refuse_new_fleet(path: Path, folder: _Folder) -> None:
    """`--new-fleet` found something besides Syncthing's own bookkeeping."""
    if folder.error is not None:
        _unreadable_folder(path, folder.error)
    where = safe_terminal_str(path)
    if folder.in_flight:
        fix = "Wait for delivery to finish, then run 'mm init'."
        what = f"is still receiving files from Syncthing ({_entry_names(folder.in_flight)})"
    elif folder.fleet:
        fix = "This folder already holds a fleet: run 'mm init' to join it."
        what = f"already holds fleet files ({_entry_names(folder.fleet)})"
    elif not folder.other:
        fix = "Run 'mm init' to join it."
        what = "now holds a key header (mm-crypto-init)"
    else:
        fix = (
            "Move them out or choose an empty folder, then run 'mm init --new-fleet'. "
            "Nothing was deleted."
        )
        what = f"holds other files ({_entry_names(folder.other)})"
    core._error(
        f"init: --new-fleet needs an empty folder, but {where} {what}. Nothing was written. {fix}"
    )


def _stop_for_missing_header(
    path: Path, folder: _Folder, *, accepted: bool = False, local_only: bool = False
) -> None:
    """Plain `mm init` found no key header: say why and what to do; write nothing."""
    where = safe_terminal_str(path)
    first_machine = (
        " Only the fleet's first machine runs 'mm init --new-fleet', in an empty folder."
        if folder.accepts_new_fleet
        else ""
    )
    if folder.error is not None:
        _unreadable_folder(path, folder.error)
    if local_only and folder.accepts_new_fleet:
        what = (
            f"Syncthing's folder at {where} has no peer and no key header "
            "(mm-crypto-init), so nothing can deliver one. Nothing was written. "
            "To finish starting this fleet, Run: mm init --new-fleet"
        )
    elif accepted and not folder.in_flight and not folder.fleet and not folder.other:
        what = (
            f"Syncthing has accepted the fleet folder at {where}, but the key header "
            "(mm-crypto-init) has not arrived yet. No device entry, config or key "
            "header was written. Wait for Syncthing to deliver it, then run "
            "'mm init' again."
        )
    elif not folder.exists:
        what = (
            f"{where} does not exist, so there is no key header (mm-crypto-init) to join. "
            "Nothing was written. Accept the shared folder in Syncthing at this path, or "
            "run 'mm init' again and enter the path Syncthing uses; once delivery "
            "finishes, run 'mm init' again."
        )
    elif folder.in_flight:
        what = (
            f"{where} is still receiving files from Syncthing "
            f"({_entry_names(folder.in_flight)}) and the key header (mm-crypto-init) has "
            "not arrived. Nothing was written. Wait for delivery to finish, then run "
            "'mm init' again."
        )
    elif folder.fleet:
        what = (
            f"{where} holds fleet files ({_entry_names(folder.fleet)}) but no key header "
            "(mm-crypto-init). Nothing was written. Wait for Syncthing to finish "
            "delivering it, then run 'mm init' again. If the folder is up to date and "
            "the header never arrives, restore it from another machine; "
            f"see {KEY_HEADER_COPIES_URL}."
        )
    elif folder.other:
        what = (
            f"{where} holds {_entry_names(folder.other)} but no key header (mm-crypto-init). "
            "Nothing was written. If this folder should join an existing fleet, wait for "
            "Syncthing to deliver the header, then run 'mm init' again."
        )
    else:
        what = (
            f"the key header (mm-crypto-init) is not in {where} yet. Nothing was written. "
            f"{first_machine.strip()} "
            "If another machine already has this fleet, wait for Syncthing to deliver "
            "it, then run 'mm init' again."
        )
    core._error(f"init: {what}")


def _write_stignore(path: Path) -> None:
    """Keep mm's temp files out of Syncthing: add one line, keep any other bytes.

    Writes nothing when the line is already present, so a rerun changes nothing.
    """
    fsutil.refuse_real_state_write_under_pytest(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = b""
    if STIGNORE_LINE.encode() in (line.strip() for line in existing.splitlines()):
        return
    separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
    fsutil.atomic_write_bytes(path, existing + separator + STIGNORE_LINE.encode() + b"\n")


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
    """Gate a re-init on a configured machine. Exits 0 when declined.

    Orphan case: this machine already has a device entry, the key header is ok
    and the folder holds state. A new device_id gets minted; old blobs remain
    readable under the shared root_salt but the old device entry no longer
    pushes or pulls. A fresh machine joining a populated folder has nothing to
    orphan and is not asked. Non-TTY aborts.
    """
    any_storage = (
        occupancy.has_any_blobs or occupancy.has_any_manifests or occupancy.has_any_devices
    )
    if not (existing_device_id and occupancy.has_crypto_init and any_storage):
        return
    msg = (
        f"This creates a new device entry, orphaning existing device "
        f"'{existing_device_id}' ({existing_device_name or 'unknown'}). "
        f"Old blobs remain readable under the shared root_salt but the "
        f"old device entry will no longer push or pull. Proceed?"
    )
    if not typer.confirm(msg, default=False):
        console.print("Device registration cancelled.")
        raise typer.Exit()


def _prompt_sources() -> list[dict[str, Any]]:
    """Prompt for each known source type; return the enabled entries.

    User-facing sources (claude, gstack) become a Y/n prompt via
    `_prompt_source_toggle`. Default is Y when the qualifying source tree
    exists on disk, N otherwise; Grok and Muse always default to N.
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
        # Grok and Muse are opt-in even when detected: enabling the source also
        # consents to the usage reader.
        default_on = exists and default["name"] not in ("grok", "muse")
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


def _prompt_passphrase(new_fleet: bool) -> str:
    """Prompt for the encryption passphrase. Double-prompt for a new fleet.

    Exits via _error on empty input or (new fleet) mismatch. Returns
    the validated passphrase string — caller uses it for bootstrap or
    verify but MUST NOT commit to keyring until crypto validation passes.
    """
    passphrase = typer.prompt("Encryption passphrase", hide_input=True)
    if not passphrase:
        core._error("Passphrase cannot be empty.")
    if new_fleet:
        confirm = typer.prompt("Confirm passphrase", hide_input=True)
        if passphrase != confirm:
            core._error("Passphrases don't match.")
    return passphrase


def _bootstrap_new_fleet(
    backend: LocalBackend, passphrase: str, full_path: Path
) -> tuple[bytes, int, bytes]:
    """Create the fleet's key header in an empty folder; return its parameters.

    This is the only writer of mm-crypto-init. Order: recheck the folder,
    write `.stignore`, then the exclusive create. A header that arrived during
    the prompts, or that wins the exclusive create, refuses instead of
    verifying: this machine was not joining.

    Sets the crypto session as a side effect so downstream calls can
    derive blob-level keys.
    """
    folder = _classify_folder(full_path)
    if not folder.accepts_new_fleet:
        _refuse_new_fleet(full_path, folder)
    try:
        arrived = fetch_crypto_init(backend)
    except OSError as e:
        _unreadable_folder(full_path, e)
    if arrived.newer_version is not None:
        core._error(core._newer_format_message(arrived.newer_version))
    if arrived.status != "missing":
        _refuse_new_fleet(full_path, _classify_folder(full_path))
    try:
        _write_stignore(full_path / ".stignore")
    except (OSError, StorageError) as e:
        core._error(
            f"init: could not write .stignore in {safe_terminal_str(full_path)}: "
            f"{os_error_cause(e)}. No key header was created. Make the folder writable, "
            "then run 'mm init --new-fleet' again."
        )
    try:
        bootstrap = bootstrap_crypto_init(
            backend, passphrase, argon2_memory_kb=DEFAULT_ARGON2_MEMORY_KB
        )
    except (OSError, StorageError) as e:
        try:
            arrived = fetch_crypto_init(backend)
        except OSError:
            # A failed diagnostic read must not mask the creation failure.
            arrived = None
        if attemptlog.storage_errno(e) == "EEXIST" or (
            attemptlog.storage_errno(e) is None
            and arrived is not None
            and arrived.status != "missing"
        ):
            if arrived is not None and arrived.newer_version is not None:
                core._error(core._newer_format_message(arrived.newer_version))
            core._error(
                f"init: another machine's key header arrived in {safe_terminal_str(full_path)} "
                "first, so none was created here. Run 'mm init' to join it."
            )
        core._error(
            f"init: could not create the key header in {safe_terminal_str(full_path)}: "
            f"{os_error_cause(e)}. No key header was created. Fix that, then run "
            "'mm init --new-fleet' again."
        )
    try:
        arrived = fetch_crypto_init(backend)
    except OSError as e:
        core._error(
            f"init: the key header was created in {safe_terminal_str(full_path)}, but "
            f"could not be read: {os_error_cause(e)}. Fix that, then run 'mm init' to join it."
        )
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


def _verify_join(
    backend: LocalBackend, passphrase: str, fetch: CryptoInitFetch
) -> tuple[bytes, int, bytes]:
    """Verify the passphrase against the fleet's key header; write nothing.

    Failure aborts via _error; no local state is written. Only after a
    successful verification does the shared repair run (it removes conflict
    copies identical to canonical and writes no key header).

    Sets the crypto session as a side effect so downstream calls can
    derive blob-level keys.
    """
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

    Only the join path calls this helper. A new-fleet exclusive-create loss
    refuses; the owner must explicitly rerun plain init to join.
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
      * register fails → re-raise; nothing local was written, and retrying
        plain init joins the existing key header.
      * save_config fails → best-effort `backend.delete(devices/<id>.json)`
        before re-raising. Normal save failures (disk full, permissions)
        self-clean so retry init doesn't trip `_init_storage_guard`'s
        orphan-case warning. If the cleanup itself fails, the original
        save error propagates — masking the real cause behind a confusing
        secondary error is worse than the cosmetic orphan that remains.
      * SIGKILL between register and save_config → cosmetic orphan device
        entry in storage. A fresh device_id is minted on retry; the orphan
        prompt applies only when this machine already has a saved device ID.
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


def _overwrite_prompt(new_fleet: bool) -> str:
    """The confirm shown when a config already exists."""
    message = f"Config already exists at {_config_module.CONFIG_PATH}. Overwrite?"
    if not new_fleet:
        return message
    try:
        prior = load_config()
    except MindMeldError:
        return message
    path = prior.get("storage", {}).get("path")
    fingerprint = prior.get("crypto", {}).get("root_salt_fp")
    return (
        f"Config already exists at {_config_module.CONFIG_PATH} (storage "
        f"{safe_terminal_str(path or 'unknown')}, root_salt fp "
        f"{safe_terminal_str(fingerprint or 'unknown')}). --new-fleet starts a separate "
        "fleet with its own key header. Overwrite?"
    )


def _pause(seconds: float) -> None:
    """Wait between header poll attempts; tests replace this helper."""
    time.sleep(seconds)


def _fetch_header_or_stop(backend: LocalBackend, full_path: Path) -> CryptoInitFetch:
    """Probe storage for mm-crypto-init and stop on unreadable, newer or corrupt headers."""
    try:
        fetch = fetch_crypto_init(backend)
    except OSError as e:
        _unreadable_folder(full_path, e)
    if fetch.newer_version is not None:
        core._error(core._newer_format_message(fetch.newer_version))
    if fetch.status == "corrupt":
        core._error(
            f"init: mm-crypto-init in {safe_terminal_str(full_path)} is corrupt. Wait for "
            "this folder to finish syncing, then run 'mm init' again. If it stays "
            "corrupt, restore a valid copy from another machine; never reinitialize. "
            f"For non-destructive recovery, see {NEWER_STORAGE_URL}."
        )
    return fetch


def _wait_for_header(backend: LocalBackend, full_path: Path) -> CryptoInitFetch:
    """Poll for mm-crypto-init after an accepted share; Ctrl-C stops cleanly."""
    console.print(
        "  Waiting up to 2 minutes for Syncthing to deliver the key header "
        "(mm-crypto-init); Ctrl-C stops the wait without writing Mind Meld data."
    )
    rounds = max(1, HEADER_WAIT_SECONDS // HEADER_POLL_SECONDS)
    try:
        for _ in range(rounds):
            _pause(HEADER_POLL_SECONDS)
            fetch = _fetch_header_or_stop(backend, full_path)
            if fetch.status != "missing":
                return fetch
    except KeyboardInterrupt:
        core._error(
            "init: stopped while waiting for the key header. "
            "No device entry, config.toml or key header was written. "
            "Syncthing keeps syncing the accepted folder; once it delivers the header, "
            "run 'mm init' again."
        )
    return CryptoInitFetch(status="missing")


def init(new_fleet: bool = False) -> str | None:
    """Run init; return the verified passphrase after a join, None after --new-fleet."""
    # Capture prior device metadata BEFORE any prompt so the orphan-case
    # warning can name the device about to be left behind.
    existing_device_id, existing_device_name = _load_prior_device_metadata()
    if _config_module.CONFIG_PATH.exists():
        if not typer.confirm(_overwrite_prompt(new_fleet)):
            raise typer.Exit()

    console.print(f"[bold]Mind Meld v{__version__} — init[/bold]\n")

    setup = _check_syncthing()

    # Storage path first: we need a backend to probe mm-crypto-init.
    storage_path = typer.prompt("Storage folder path", default=DEFAULT_STORAGE_PATH)
    full_path = Path(storage_path).expanduser()
    console.print(f"  Storage: {full_path}")

    # Read-only first look: nothing is created, not even the folder, until a
    # path is known to be the right one.
    folder = _classify_folder(full_path)
    if folder.error is not None:
        _unreadable_folder(full_path, folder.error)
    if new_fleet and not folder.accepts_new_fleet:
        _refuse_new_fleet(full_path, folder)
    accepted_share = None
    if not new_fleet and setup.record is not None:
        accepted_share = _managed_join(setup, full_path)
        folder = _classify_folder(full_path)
        if folder.error is not None:
            _unreadable_folder(full_path, folder.error)

    # Lightweight backend (config is not yet written; instantiate directly).
    backend = LocalBackend(str(full_path))

    # Probe storage for mm-crypto-init BEFORE committing any local state.
    fetch = _fetch_header_or_stop(backend, full_path)
    if not new_fleet and fetch.status == "missing":
        if accepted_share is True:
            fetch = _wait_for_header(backend, full_path)
        if fetch.status == "missing":
            folder = _classify_folder(full_path)
            if folder.error is not None:
                _unreadable_folder(full_path, folder.error)
            _stop_for_missing_header(
                full_path,
                folder,
                accepted=accepted_share is True,
                local_only=accepted_share is False,
            )
    if new_fleet and fetch.status != "missing":
        _refuse_new_fleet(full_path, _classify_folder(full_path))

    # Re-init guard: storage occupancy is authoritative state, not just local
    # config. Runs BEFORE any further prompt, so a refused init never touches
    # the keyring or writes config.
    if not new_fleet:
        try:
            occupancy = _probe_storage_occupancy(backend)
        except OSError as e:
            _unreadable_folder(full_path, e)
        _init_storage_guard(
            occupancy,
            existing_device_id=existing_device_id,
            existing_device_name=existing_device_name,
        )

    # Init-time collision prevention: scan existing peers and regenerate
    # the 8-char device_id on collision. UUID4 prefix collisions are
    # extremely unlikely (~1 in 4 billion per draw) but a deterministic-RNG
    # bug or a cloned-from-snapshot peer could collide reproducibly.
    # Cheap to defend in depth; runtime lookup_device_by_short_id still
    # handles a stray collision if we exhaust the retry budget.
    existing_devices = [] if new_fleet else list_devices(backend)
    device_id = generate_unique_short_device_id(existing_devices)
    device_name = typer.prompt("Device name", default=_default_device_name())

    passphrase = _prompt_passphrase(new_fleet)
    if new_fleet:
        if setup.record is not None:
            _managed_new_fleet_folder(setup, full_path)
        root_salt, argon2_memory_kb, _keycheck_blob = _bootstrap_new_fleet(
            backend, passphrase, full_path
        )
    else:
        root_salt, argon2_memory_kb, _keycheck_blob = _verify_join(backend, passphrase, fetch)
        if setup.record is not None:
            try:
                _write_stignore(full_path / ".stignore")
            except (OSError, StorageError) as e:
                core._error(
                    f"init: could not write .stignore in {safe_terminal_str(full_path)}: "
                    f"{os_error_cause(e)}. This device was not registered. Make the folder "
                    "writable, then run 'mm init' again."
                )

    # All crypto passed; it's safe to prompt for sources and write state.
    # Refuse if no sources enabled — a config with zero sources leaves
    # push/pull silently no-op'ing.
    #
    # Ordering note: source prompt runs AFTER crypto so a wrong passphrase
    # surfaces BEFORE we ask about sources, not after. Consequence: on a
    # --new-fleet refuse-all, mm-crypto-init is already written to storage
    # and orphaned. This is benign: running plain `mm init` now joins the
    # header this machine wrote, the same passphrase verifies against it, and
    # the second attempt finishes cleanly. No data loss, just a storage-side
    # breadcrumb. See tests/test_initflow.py::
    # test_new_fleet_every_source_declined_is_recoverable_by_plain_init.
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

    if new_fleet:
        if setup.record is not None:
            local_id = safe_str(setup.local_id or setup.record.device_id)
            console.print(
                "\n[green]Mind Meld initialized.[/green] This machine's Syncthing "
                f"device ID is {local_id}. Run 'mm push' here, then run 'mm init' "
                "on each other machine and paste this ID when it asks."
            )
        else:
            console.print(
                "\n[green]Mind Meld initialized.[/green] Share "
                f"{safe_str(full_path)} as the root of a Syncthing folder, run 'mm init' on "
                "the other machines, and run 'mm push'."
            )
        return None
    console.print(
        "\n[green]Mind Meld initialized.[/green] Pulling now; if the folder is still "
        "syncing, run 'mm pull' again when delivery finishes."
    )
    return passphrase


def _default_device_name() -> str:
    """Generate a default device name from hostname."""
    return socket.gethostname()
