# Init / devices / trust boundary — load-bearing invariants

Read BEFORE editing any of these:

- `src/mind_meld/cli.py` — `_register_and_save` / `_ensure_device_registered` / `init` / `_init_storage_guard` / `_do_gc`
- `src/mind_meld/devices.py` — `register_device` / `update_last_seen` / `list_devices` / `_devices_write_lock`
- `src/mind_meld/storage/local.py` — `LocalBackend.put_exclusive` / `find_conflict_copies`
- `src/mind_meld/safety.py` — `safe_str` / `safe_text` / `strip_terminal_escapes` / `safe_terminal_str`
- Anywhere a peer-controlled string is rendered (filename, path, source name, device name, `str(e)` exception tail) — use `safe_str` in Rich markup, `safe_text` for diff content, and `safe_terminal_str` for single-line plain stderr

Tests: `tests/test_devices.py`, `tests/test_safe_str.py`, `tests/test_silent_failure_contract.py`, `tests/test_storage_local.py`, `tests/test_recover.py`, `tests/test_recovery.py`.

---

## Init order + push-time self-heal (load-bearing, v0.9.4)
`_register_and_save` (renamed from `_save_and_register`) writes the remote first, the local pointer last: `register_device(backend, ...)` → `save_config(...)` → keyring store. Canonical filesystem/DB transaction discipline — a SIGKILL/OOM/power-loss in the window between the two writes leaves an inert orphan storage entry (recoverable on retry init via `_init_storage_guard`'s orphan-case prompt), never the inverse half-state where local config claims a `device_id` storage doesn't contain. Pre-v0.9.4 produced the inverse mapping. Do NOT reorder these calls.

If `save_config` raises (disk full, permissions), a best-effort `backend.delete(device_key(device_id))` cleanup runs before the original exception propagates — keeps orphans from accumulating in storage when normal save failures hit. Cleanup-failure surfaces a `mm: warning:` stderr breadcrumb but does NOT mask the original save error (visible-failure contract). The `device_key(device_id)` storage key is precomputed BEFORE `register_device` so the cleanup-warning f-string can't itself raise from `device_key`'s validation and mask the real cause (codex adversarial 2026-04-25).

`_ensure_device_registered(backend, device_id, device_name, *, dry_run)` runs at the top of `_push_core` BEFORE any push work. If `devices/<my_id>.json` is absent, it recreates it via `register_device`. Two scenarios converge: future v0.9.4+ SIGKILL crash mid-init (cosmetic) AND retroactive fix for pre-v0.9.4 victims of the v0.8.15..v0.9.3 inverted half-state — those users had been pushing manifests under an ID no peer recognized, silently. First push after upgrading to v0.9.4 self-heals. Gated on `not dry_run` (codex review: `mm push --dry-run` must not mutate storage). Register failures emit a `mm: warning:` stderr breadcrumb before re-raising — load-bearing for autopush, whose generic `except Exception` would otherwise swallow the failure and silently no-op every push.

## `register_device` create-only contract (load-bearing, v0.10.1)
Pre-v0.10.1, `register_device` always wrote `backend.put(key, ...)`. The push-time `_ensure_device_registered` self-heal (v0.9.4) called `register_device` whenever `backend.exists(key)` returned False. iCloud's `.icloud` placeholder (cloud-only, lazy-materialized) creates a TOCTOU window where `backend.exists()` reports False but the entry actually exists on storage — the self-heal re-registered, silently bumping the `registered:` first-registration timestamp on every push.

v0.10.1 routes `register_device` through `LocalBackend.put_exclusive(key, data)` (atomic `os.link` with `EEXIST` detection) so the create-only invariant holds at the filesystem layer regardless of placeholder state. Existing entries surface as `StorageError`, which the function swallows + returns. Original `registered:` timestamps are preserved across re-registration. Idempotent: self-heal callers can re-register safely.

## Devices write lock (load-bearing, v0.10.1)
`update_last_seen` does a read-modify-write of `devices/<id>.json` on every push (mutates `last_seen` + `last_seen_version`). Concurrent autopush + interactive push could race on the RMW. Today's deterministic fields (`last_seen`, `last_seen_version`) don't lose data because both writers compute the same effective state, but any FUTURE non-deterministic field (e.g. per-machine notes, error counters, partial-progress markers) would lose interleaved updates.

`_devices_write_lock()` is a `contextlib.contextmanager` wrapping the RMW in `fcntl.LOCK_EX | LOCK_NB` against `~/.config/mind-meld/devices-write.lock` (mode 0o600, parent dir auto-created). Brief retry budget on contention — `_LOCK_RETRY_INTERVALS_S = (0.05, 0.1, 0.2, 0.4)` (~750ms total before degrading). Total acquire wait stays well under 1 second since the critical section is one storage GET + one storage PUT.

On exhausted retries, degrade to executing without the lock and emit one `mm: warning: device write lock contended; skipping last_seen update for this push` line to stderr (visible-failure contract). Today's deterministic fields are safe under degraded operation; the warning lets the user catch a stuck-process scenario before any future non-deterministic field starts losing data.

The lock is LOCAL (per-machine config dir) — `fcntl.flock` is a local-process primitive and never reaches synced storage. All RMW callers MUST hold the flock for the read AND write so an interleaved read can't observe a partial state. Routing field-adders through this lock is forward-defense for concurrency safety.

## Peer-controlled string sanitization (load-bearing, v0.10.1, security)
Every synced filename AND file body crosses an untrusted trust boundary. Without sanitization, a peer can plant Rich markup (`[/red]…[red]`) or terminal escape sequences in any synced filename or file body and have them rendered as control output during `mm pull` / `mm conflicts` / `mm resolve` / `mm devices` / `mm status`. The OSC 52 vector is particularly nasty — many terminals (xterm, iTerm2, kitty, alacritty) honor base64-encoded clipboard writes from remote-controlled escape sequences, silently changing the user's clipboard. CSI `\x1b[2J` clears the screen; OSC 0/2 spoofs the title; DCS / C1 8-bit are also covered.

`_strip_terminal_grammar` (private) removes the full common-grammar set: CSI `\x1b[…[\x40-\x7e]`, OSC `\x1b]…(BEL|ST)`, DCS, single-byte `\x1b[\x40-\x5f]` (SS2/SS3 and other C1-index finals; RIS `ESC c` is not in that range), and the rarely-used 0x9b 8-bit C1 CSI variant. That intermediate is not safe to render: one regex pass can assemble a fresh OSC by deleting an inner CSI.

Public `strip_terminal_escapes(s)` runs the grammar helper, then deletes every residual ESC (`U+001B`) and C1 (`U+0080`–`U+009F`). Returned text is ESC/C1-free. This is not an all-control guarantee: LF and HT survive (required for `safe_text` diff bodies), and other C0/DEL/Unicode-format characters are left to the sink. Apply BEFORE rendering any peer-controlled string to a real terminal — Rich's `Text()` does NOT strip these.

`safe_str(s)` composes public `strip_terminal_escapes` with `rich.markup.escape` and returns a plain `str`, so f-string composition with Rich markup tags continues to work: `f"[red]write failed:[/red] {safe_str(rel_path)}"`. Use at Rich markup print sites interpolating a peer-controlled string (filenames, paths, source names, device names, error message tails — including exceptions whose `str(e)` echoes peer-supplied bytes).

`safe_terminal_str(value)` is the helper for single-line plain stderr (Track 50A, v0.14.4; Track 52A, v0.14.6, keeps its visible notation). It stringifies, runs the private grammar helper (not the public residual-deletion path), then keeps each remaining character only if `str.isprintable()` is true; otherwise it renders `ascii(ch)[1:-1]`. That visibly escapes residual C0/DEL/C1, Unicode line separators, format controls (including ZWJ, so some joined emoji spellings appear split), and surrogate codepoints, while preserving printable Unicode and literal brackets. Residual ESC/C1 therefore appear as printable ASCII notation, which already satisfies the forbidden-codepoint property. Display text is not a filesystem identity or a shell argument — callers keep the original Path / storage key / model id for validation, lookup, recovery, and deletion. Rejected storage-filename warnings in `LocalBackend.find_conflict_copies` use this helper directly; the malformed-blob and verbose-orphan GC warnings compose `safe_str(safe_terminal_str(bkey))` because those sinks are Rich markup. Original storage keys are still used for parse, lookup, and deletion. Track 52A also routes the token-cache GC failure notice, the oversize-jsonl-line notice, and the unknown-model pricing notice through this helper; remaining plain-stderr `safe_str` sites are a recorded follow-up.

`safe_text(s, **kwargs) -> rich.text.Text` is the diff-content variant. Use for diff CONTENT lines (peer-controlled file bytes printed via `console.print`). `Text()` alone defangs Rich markup but passes raw ANSI/OSC/DCS through to the terminal. It calls public `strip_terminal_escapes` first, then forwards `**kwargs` to `Text()`. `Text.plain` is ESC/C1-free; LF and HT remain exact. Caller-supplied Rich styles/kwargs are trusted application formatting.

Sweep covers ~30 print sites: pull-prediction widget, upload progress, conflict prompts, write/merge/conflict apply paths, all `_apply_*` / `_pull_*` error tails, `mm devices` table cells, `mm diff` per-source headers, fleet-version refusal listing, `_print_pull_summary` warnings, `_resolve_interactive_loop` headers + prompts + diff labels + diff content + outcome lines. `mm devices` Rich Table cells are sanitized too — Table cells interpret markup AND pass raw escapes through (verified). All sites pinned in `tests/test_safe_str.py`.

**v0.11.1 extension — banner trust boundary covers `device_name` too.** The conflict-prompt LOCAL/REMOTE banners (Track 12A, conflictdiff.py) interpolate two peer-controlled strings: the conflict filename AND the peer's `device_name` (set via `typer.prompt` at peer init, plaintext-synced via `devices/<id>.json`, and rendered as `(from <peer_name>)` on the REMOTE banner). `render_banner` wraps both inputs in `safe_text` BEFORE composition so a peer planting OSC 52 / CSI / DCS in their own `device_name` cannot reach the terminal of any peer that pulls. Codex outside-voice review (T6) caught this — pre-v0.11.1 the sanitization sweep covered filenames but not `device_name`. Pinned by `tests/test_safe_str.py::TestConflictBannerSanitization`.

**v0.11.1 module move.** `safe_str`, `safe_text`, `strip_terminal_escapes` live in `mind_meld.safety` (extracted from cli.py to break the cli↔conflictdiff circular import). cli.py re-exports the names for backwards compat; new tests should import from `mind_meld.safety` directly.

## `store_passphrase_in_keyring` PYTEST_CURRENT_TEST guard (load-bearing, v0.11.11)
A test-fixture passphrase (`pw123`) reached a real fleet's macOS Keychain through a path that bypassed conftest's `_isolate_keyring` autouse fixture (most likely a non-pytest harness or manual init against a placeholder storage path), wedging `mm pull` until the user manually overwrote the entry. Production `crypto.store_passphrase_in_keyring` now short-circuits to `False` when `os.environ.get("PYTEST_CURRENT_TEST")` is truthy. pytest sets that variable on every test phase and it's inherited by subprocesses, so any test-orchestrated path (in-process, subprocess, `python -c '...'` from a test, REPL under pytest) physically cannot reach `keyring.set_password` against the real macOS Keychain. Real-CLI `mm init` from a user shell is unaffected. Failure mode under the guard is the existing "[yellow]No keyring available[/yellow]" warning path — recoverable via `MINDMELD_PASSPHRASE` env var or re-running init in a clean env, strictly better than the silent passphrase poisoning the guard prevents. Existing `TestStorePassphraseInKeyringExceptNarrow` pins `monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)` so they exercise the real-CLI write path past the guard; new `TestStorePassphrasePytestGuard` pins both branches.

## Verified crypto repair and read-only inspection (Tracks 56A/60A)

### Newer format refusal (Track 66A, v1.0.0)

`crypto.FORMAT_VERSION_MAX` reserves 0x03–0x0F for future crypto-init and blob
versions. Read the version byte before assuming a layout, even on a one-byte
envelope. A current-format init with a keycheck in that window is newer too.
Empty/truncated v2 and bytes above 0x0F remain corrupt; ASCII garbage beside a
valid copy still self-heals. If **any** observed canonical or conflict copy is
newer, `fetch_crypto_init` returns `corrupt` with `newer_version` set, no winner
and no repair plan. Every crypto session refuses. Status reports the refusal;
diag remains an inspection and exposes `crypto_init.newer_version` (null when
none). Neither uses a newer copy's assumed salt or keycheck layout.

`apply_crypto_init_repair` re-fetches even with no pending plan; fresh newer
evidence raises `NewerFormatError` before writing, preserving, or unlinking.
The CLI adds the single upgrade remedy using `upgrade.INSTALL_CMD`; crypto
never imports upgrade. Autorun keeps exit 0 and the `crypto-error` breadcrumb.
Newer blobs raise the same typed `CryptoError` subclass; per-file pull warnings
add the upgrade remedy. `_fetch_remote_manifest` and `_make_manifest_validator`
deliberately fold that subclass into corrupt-manifest handling. A manifest
copy whose first byte is in 0x03–0x0F is unreadable even when an older sibling
still decrypts, and a newer conflict copy is not "missing". A read that sees
that byte after the preliminary scan, including the conflict validator's own
read, still returns corrupt instead of using the older sibling. Pull skips
that peer and GC refuses. Push and manifest recovery refuse before replacing
this Mac's own newer manifest. Blobs only the unread copy could name are kept.

iCloud does not order arrival. A format-changing MAJOR must write its new
crypto-init **before** new-format manifests/blobs, and refuse on the newer side
while any registered peer runs below 1.0.0 (0.14.x lacks this gate). A manifest
or blob may still arrive first: pull skips an unreadable peer manifest with a
warning, GC refuses while any manifest is unreadable, and push writes only its
own manifest. Init rescans immediately before bootstrap and again before
accepting a new salt, so a newer copy that arrives during the prompts refuses
without saving config. This is an optimistic local recheck, not a distributed transaction.
See [Compatibility (1.x)](auto-upgrade.md#compatibility-1x).

Tests: `TestNewerFormats66A` in `tests/test_crypto.py` (including newer canonical
plus valid older copy, byte-for-byte preservation and late arrival),
`TestNewerStorage66A` in `tests/test_integration.py` (command refusal, bootstrap
race, arrival window and blob remedy), and `test_crypto_init_newer_version66a`
in `tests/test_diag.py`.

### Current-format reconciliation

`fetch_crypto_init(backend)` is pure; there is no `repair` parameter. It selects the lex-smallest-salt valid candidate (canonical first on ties), retains the exact winning bytes, and returns a frozen `CryptoInitRepairPlan`. The plan binds each canonical/conflict candidate's name and content hash to `delete` for byte-identical conflict copies or `preserve` for distinct/unreadable bytes. Different salt, memory parameters, keycheck, or malformed content are all preserved. Canonical and conflict copies are read with `O_NOFOLLOW` as regular files; a symlink or other unreadable name has no digest authorizing removal or a preserve-copy into storage. An unreadable canonical refuses repair.

`_init_crypto_session` orders pure fetch → fingerprint drift check → load key → verify passphrase → `crypto.apply_crypto_init_repair` for mutating sessions. The repair function re-fetches and aborts with `CryptoError` when the exact winner changed, even if there was no pending repair. It rechecks the canonical hash before replacing it, preserves displaced canonical bytes, then writes the winner to canonical with file and directory fsync. Each differing readable conflict is written and fsynced under `mm-crypto-init.preserved-<fp8>-<utc>` before unlink; identical conflicts are deleted only after all required writes/fsyncs succeed. Names are re-listed and intersected with the plan, never joined from stored strings. Changed or newly arrived copies survive. Before that unlink pass, a whole-repair recheck confirms canonical still holds the exact winner bytes this process just published; if a second concurrent repair has already replaced canonical again in the interim, every remaining conflict copy is left in place rather than deleted, so the losing side of that second race can never erase the only surviving copy of the winner it lost to (pinned by `tests/test_crypto.py::TestConflictConvergence::test_concurrent_canonical_replacement_retains_conflict_copies`). A write/fsync failure leaves every conflict candidate in place; preserved files lie outside the conflict pattern. Rechecking is optimistic local filesystem coordination, not an iCloud-wide transaction.

First-device bootstrap remains create-only. Init's storage probe is pure; second-device and bootstrap-race retry paths verify before applying repair, and a changed winner aborts before config writes or device registration. An unreadable canonical cannot be safely preserved and refuses repair.

Inspection commands `status`, `diag`, and `diff`, plus `push --dry-run`, `pull --dry-run`, `gc --dry-run`, and `recapture --dry-run`, never repair shared crypto-init state. Status loads `_get_config(read_only=True)` and never persists a missing fingerprint; read-only crypto sessions backfill only in memory. Status and the previews print pending counts; diag reports `crypto_init.pending_repair` (`replace_canonical`, `delete`, `preserve`), never copy names. Drift errors include pending counts in every mode. The pending note names the next push, pull, autopull, or autopush that verifies the passphrase as a reconciliation trigger. Track 62A also gates their other setup writers; see the [README Previews table](../../README.md#previews). Every inspection loads config read-only, and declared status/identity-cache exceptions never permit shared crypto repair.
