# Conflict resolution — load-bearing invariants

Read BEFORE editing any of these:

- `src/mind_meld/cli.py` — `_apply_conflict` / `_apply_incoming_file` / `_resolve_interactive_loop` / `_prompt_conflict_choice` / `_check_fleet_version_or_refuse` / `_find_conflict_files`
- `src/mind_meld/conflictdiff.py` — `render_prompt` / `render_banner` / `count_divergent_lines`
- `src/mind_meld/merge.py` — `lcs_merge` / `merge_file` / `should_merge`
- `src/mind_meld/manifest.py` — `parse_conflict_device_short` / `is_conflict_filename` / `is_pre_inversion_conflict_filename`
- `src/mind_meld/devices.py` — `lookup_device_by_short_id` / `generate_unique_short_device_id`

Tests: `tests/test_conflict_copy.py`, `tests/test_conflictdiff.py`, `tests/test_merge.py`, `tests/test_safe_str.py::TestConflictBannerSanitization`.

---

## Conflict-direction inversion + fleet-version refusal (load-bearing, v0.9.2 BREAKING)
`_apply_conflict` keeps LOCAL bytes at canonical; REMOTE bytes go to `.sync-conflict-*` sidecar. No rename + rollback dance — local is never overwritten in the conflict path. Pre-v0.9.2 produced the opposite mapping; pre-existing files migrate to a `v0-` prefix on first lock-protected discovery in `mm pull` / `mm resolve` (NEVER from `mm conflicts` — lockless, would race autopull, codex-2 #5).

`_resolve_interactive_loop` is dual-mode dispatched BY FILENAME PREFIX (not timestamp — sound, since post-v0.9.2 code never produces a `v0-` file directly). `v0-` files: `(l)ocal` renames sidecar over canonical, `(r)emote` unlinks sidecar. No-prefix files: `(l)ocal` unlinks sidecar, `(r)emote` renames sidecar over canonical. Diff fromfile/tofile labels flip per row to match.

## Conflict-prompt UX (load-bearing, v0.11.1 BREAKING — interactive prompt)

The two interactive prompt sites (`_resolve_interactive_loop` post-pull walk in cli.py:5688, `_prompt_conflict_choice` inline pull-time in cli.py:1115) share leaf primitives in `src/mind_meld/conflictdiff.py`: `render_prompt`, `render_banner`, `count_divergent_lines`. Site-level dispatch over the four shapes (canonical-exists × pre-inversion / post-inversion × canonical-missing) stays at each call site — burying it in a helper would hide the load-bearing filename-prefix dispatch.

**`(b)oth` → `(s)kip` rename + alias.** Default key changed from `b` to `s` in v0.11.1. Same on-disk effect — both leave the canonical and `.sync-conflict-*` files in place — but the option name now matches the action. The pre-1.0 letters `b` / `both` are aliased to skip with a one-time `mm: notice:` so stale scripts continue to work; alias removes at 1.0. **Exact-match dispatch** (`if choice in ("b", "both")`): `back`/`browse`/`between` must NOT silently trigger the alias.

The pre-v0.9.0 letters `c` / `f` remain LOUD-rejected (real silent-data-loss risk in mapping them through post-inversion). The asymmetry is deliberate: `c`/`f` encoded directional ambiguity that the v0.9.2 inversion broke; `b` does not.

**Honest skip-lifecycle copy.** The `(s)kip` line reads `leave both files on disk; run `mm resolve` later or delete manually` — explicit that the next pull does NOT re-prompt unless remote changes again, so the conflict file persists indefinitely. Codex outside-voice review (T2) caught the misleading prior wording ("decide on the next pull").

**Three-number divergence summary.** `count_divergent_lines` counts `-` / `+` lines in the unified diff (excluding the `---` / `+++` headers) and returns `(M, N, K)` where M = removed-or-replaced, N = added-or-replaced, K = M + N. Wording is honest about replacement semantics: a 1-line replacement is M=1, N=1, K=2 (counting both old and new). Codex T1 caught the original "unique to local/remote" wording as pseudo-precision.

**Banner attribution chain.** `parse_conflict_device_short(name)` (manifest.py) extracts the 8-char device prefix from the conflict filename. `lookup_device_by_short_id(devices, short_id)` (devices.py) is a pure function over the existing devices list returning `(device | None, count)`:
* `(None, 0)` — no peer matches; banner shows `(unknown peer)`.
* `(device, 1)` — exact attribution; banner shows `(from <device_name>)`.
* `(None, N)` for N > 1 — collision; banner shows `(ambiguous -- N peers match this prefix)` AND emits a one-shot per-prefix `mm: notice:` to stderr (forensic breadcrumb for fleet-config issues; codex T4 caught the stderr-only-is-too-quiet hazard).

**Device list cache hoisted.** `mm resolve` calls `list_devices(backend)` ONCE before entering the walk and threads the resulting list into `_resolve_interactive_loop`. iCloud cold-cache reads can stack to multi-second per `list_devices` call; without hoisting, an N-conflict walk would N+1 on storage. The same pattern flows through `_pull_one_source` → `_download_and_apply` → `_apply_incoming_file` → `_prompt_conflict_choice` for the inline pull-time prompt.

**Init-time device-id collision regenerate.** `generate_unique_short_device_id(devices, max_retries=5)` (devices.py) draws `uuid.uuid4().hex[:8]` and retries on collision against the existing fleet. After exhaustion, returns the last drawn id and emits `mm: warning:` — runtime `lookup_device_by_short_id` defends in depth via the multi-match path, so a colliding install is degraded for attribution but not catastrophic. Wired into `_register_and_save` at init; runtime helper handles legacy collisions on already-registered fleets.

**`Literal['pre_inversion', 'post_inversion']` not `bool`.** `render_prompt(canonical, conflict, mode)` takes a typed mode string, not a True/False flag. A boolean that flips canonical/sidecar semantics is exactly the footgun class the v0.9.2 inversion section warns about; codex T6 caught the smell. Matches existing pattern (`ManifestFetch.status: Literal['ok','missing','corrupt']`).

**Inline pull-time prompt is post-inversion only.** `_prompt_conflict_choice` is called from `_apply_incoming_file` during pull, BEFORE `_apply_conflict` writes the sidecar — and `_apply_conflict` (post-v0.9.2) only ever produces post-inversion files. Pre-inversion files surface only later, in `mm resolve`'s discovery walk. So the inline path passes `mode="post_inversion"` unconditionally; the four-shape dispatch lives in `_resolve_interactive_loop` only.

## `(m)erge` option + `lcs_merge` LCS-as-synthetic-base 3-way merge (load-bearing, post-v0.11.3 — pure-Python user-confirmed merge)

`mind_meld.merge.lcs_merge(local_bytes, remote_bytes) -> tuple[bytes, int]` runs at BOTH conflict-prompt sites BEFORE the prompt renders: `_resolve_interactive_loop` (post-pull) and `_prompt_conflict_choice` (inline pull-time). Returns `(merged_bytes, conflict_count)` where `conflict_count == -1` means "binary input, do not offer the option," `0` means "clean merge, offer (m) as the default key," and `> 0` means "merged candidate contains git-style `<<<<<<<` markers; offer (m) but keep the default at `s` so the user must affirmatively pick it."

**Algorithm.** `difflib.SequenceMatcher(autojunk=False).get_opcodes()` over `local.splitlines()` and `remote.splitlines()`. Implicitly treats the longest common subsequence as the shared ancestor: `equal` runs are kept; `delete` (lines in local-only) and `insert` (lines in remote-only) are kept under the lossless additive interpretation; `replace` runs become `<<<<<<< local / ======= / >>>>>>> remote` markers. Without a stored last-synced hash (the deferred Future TODO "Three-way merge base") this is the conservative interpretation that never silently loses content — the only data-loss vector is misalignment on pathological inputs (lots of repeated short lines), and that surfaces as visible markers the user rejects.

**Inversion-aware argument order.** `_resolve_interactive_loop` walks `v0-` (pre-inversion) and post-inversion sidecars in the same loop. Pre-inversion: canonical = remote bytes, cpath = local bytes; the call is `lcs_merge(cpath_bytes, canonical_bytes)`. Post-inversion: canonical = local bytes; the call is `lcs_merge(canonical_bytes, cpath_bytes)`. Marker labels (`<<<<<<< local`, `>>>>>>> remote`) stay accurate either way because the first arg is always the local side. The inline pull-time prompt is post-inversion only and passes `lcs_merge(local_bytes, remote_data)`.

**Render side.** `conflictdiff.render_prompt(canonical, conflict, mode, *, merge_available=False, merge_conflicts=0)` adds the `(m)erge` line when `merge_available=True`. Annotation differs by conflict count: `clean, no markers` for 0; `contains N <<<<<<< region(s); resolve in editor after` for N > 0. Default key flips to `m` only when `merge_available and merge_conflicts == 0`. Mode-symmetric: works for both pre-inversion and post-inversion files because the merge primitive itself is symmetric and the prompt just describes the action.

**Apply side.** On `(m)`: `fsutil.atomic_write_bytes(canonical, merged_bytes, fsync=False)` writes the merged result over canonical; the sidecar is best-effort `unlink()`-ed. Unlink-failure surfaces a `mm: warning: merged result written; sidecar unlink failed: <name> — <e>` to stderr but does NOT roll back canonical (the merge succeeded; the sidecar is cosmetic, reaped by `mm gc --conflicts` 30-day TTL). Outcome string is `merged-via-lcs` (distinct from `merged` for the `.jsonl` / `MEMORY.md` line-union path) so `mm log --action merged-via-lcs` post-dogfood gives an honest count.

**Refusal of `(m)` when not offered.** If the user types `m` / `merge` but `merge_available` is False (binary content), the prompt site treats the literal letter as `keep-both` — equivalent to skip. Without this, a binary file with NUL bytes in either side would write empty bytes (`merged_bytes = b""`) over canonical and silently truncate the file.

**Trailing-newline preservation.** `lcs_merge` splits without `keepends` (so trailing-newline variations don't trip the LCS into a spurious `replace` on the only line of a file) and re-attaches a `\n` terminator on output if either input had one. Memory entry files routinely end with `\n`; the merged result matches.

**Future graduation path.** The user-confirmed (m) prompt is the conservative ship for the dogfood window. If clean-merge accepts dominate during dogfood, the dispatch in `_apply_incoming_file` can flip to "silently apply lcs_merge result at pull time when conflict_count == 0" (Approach A in the /plan-ceo-review). Same `lcs_merge` primitive, no new module.

`_check_fleet_version_or_refuse(backend, my_device_id)` runs at the top of `_pull_core` BEFORE any I/O. Per-peer classification via `packaging.version.Version` against `INVERSION_MIN_VERSION = "0.9.2"`: safe (>= 0.9.2 → ALLOW), inactive (last_seen missing → ALLOW), pre-v0.9.2 (last_seen present, version missing or < threshold → REFUSE), dropped (corrupt device.json → REFUSE by storage key). Refusal message names every offending peer; recovery is `pip install --upgrade mind-meld` + `mm push` on each peer. Implementation uses `list_devices_with_drops` (silent variant) so the `_select_devices`-side `_list_devices_warn` only logs once if the fleet check passes.

`update_last_seen` writes `last_seen_version: __version__` alongside `last_seen` on every push. Forward-compatible (older mm tolerates unknown keys). `mm devices` table surfaces it as a column.

## `resolve(local)` mtime bump (load-bearing, v0.12.5)

`_resolve_interactive_loop`'s `(l)ocal` branch MUST stamp the canonical with an mtime strictly greater than the peer's mtime via `_bump_canonical_mtime_post_resolve(canonical, peer_mtime)`. Without the bump, the user's "I picked local" decision is silent on disk: canonical keeps its old mtime (which `_apply_incoming_file`'s mtime gate at `cli.py:1633` just classified as `<=` peer's, else the conflict path wouldn't have fired), the sidecar dedup signal from `_existing_post_inversion_sidecars_from_peer` is gone (just unlinked), and the next pull from the same peer re-runs the conflict path and writes a fresh sidecar. Users hit a `resolve → pull → resolve → pull` loop with no on-disk signal that they keep choosing local.

`peer_mtime` is read BEFORE the unlink/rename: post-inversion from the sidecar (whose mtime was restored from the peer's manifest by `_restore_mtime_best_effort` during the producing pull); pre-inversion from the original canonical (which held peer/remote bytes before the v0- sidecar promotion). Stat failure degrades to `peer_mtime = 0.0` so the bump still happens — worst case the canonical's mtime is `now`, which is still > any peer's past mtime in practice.

Future-clamp symmetry. The bump caps target at `now + _MTIME_RESTORE_MAX_SKEW_SECONDS` (the same 60s ceiling `_restore_mtime_best_effort` enforces on inbound mtimes) so downstream peers don't re-clamp our pushed mtime and create a fresh divergence. Edge case: when peer's mtime is itself at the cap (post-clamp upper bound), `max(now, peer_mtime + 1.0)` lands at `peer_mtime + 1.0` which then gets clamped back to `now + 60s = peer_mtime`. Local-mtime is not strictly greater in that single case; the cycle persists one more pull and self-heals on peer's next legitimate push (when peer's mtime returns to normal). Not worth a non-symmetric "violate the clamp by 1s locally" workaround.

Cross-fleet propagation story (the load-bearing why). After bump: this device's next push publishes a manifest entry with the bumped mtime and local-canonical's bytes. Other peers pull: they see `peer_mtime (bumped) > their_local_mtime`, hit the conflict path against THEIR local bytes (which differ from us), write a sidecar of OUR bytes, and their user picks `(r)emote` — or `(l)ocal` again, looping us back, which means the user disagrees with us and we resolve the disagreement consciously. Either way the fleet converges instead of cycling indefinitely on the original publisher.

Best-effort `os.utime`. `OSError` / `OverflowError` is swallowed: the bump is a propagation hint, not a correctness gate. The user-visible resolve action (rename or unlink) already succeeded by the time the bump runs; failing the resolve over a utime failure on an iCloud placeholder would be a regression in user trust. Pinned by `tests/test_conflict_copy.py::TestResolveLocalMtimeBump` (post-inversion bump, pre-inversion bump, end-to-end no-recurrence, future-clamp cap).

## `_find_conflict_files` tuple-key dedup (load-bearing, v0.9.4 + v0.10.1)
The function runs two scan strategies that overlap when an `include_files` entry sits inside an `include_dirs` directory: (1) `include_dirs` rglob and (2) depth-0 sibling-glob for `include_files`. Without dedup, a conflict file at e.g. `projects/notes.sync-conflict-...md` is visited twice when a user customizes config with `include_files: ["projects/notes.md"]` AND `include_dirs: ["projects"]` (nested) — duplicate rows in `mm conflicts`, inflated counts in `mm gc --conflicts`, `mm resolve` silent no-op on the second visit. Default config doesn't trigger this (all `include_files` are bare top-level dotfiles), but the dedup is footgun-removal for anyone customizing.

v0.9.4 keyed dedup on `set[tuple[str, Path]]`. v0.10.1 strengthened the key to filesystem identity: `(src_name, st_dev, st_ino)` when stat succeeds, `(src_name, str(path))` fallback when stat fails (race window between glob and dedup — never silently drop a conflict file just because of a transient stat error). Filesystem identity handles the case-mismatched-config-on-APFS hazard (`include_dirs: ["projects"]` AND `include_files: ["Projects/notes.md"]` resolve to the same inode but distinct path strings; bare-string keys would let both through). The `src_name` component still preserves source attribution when two configured sources legitimately reference overlapping subtrees. Pinned in `tests/test_conflict_copy.py::TestFindConflictFilesNestedDedup` (v0.9.4) and `TestFindConflictFilesIdentityDedup` (v0.10.1).
