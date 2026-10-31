# Sync mechanics — load-bearing invariants

Read BEFORE editing any of these:

- `src/mind_meld/pullflow.py` — `_pull_core`
- `src/mind_meld/cli.py` — `_push_core`
- `src/mind_meld/core.py` — `_SyncProgress` / `_start_sync_progress`
- `src/mind_meld/core.py` — `_pause_sync_progress` / `_fetch_remote_manifest`
- `src/mind_meld/core.py` — `_recover_prior_manifest`
- `src/mind_meld/core.py` — `_filter_excluded_paths` / `_filter_disabled_sources`
- `src/mind_meld/pullflow.py` — `_drop_case_collisions_from_manifests`
- `src/mind_meld/fsutil.py` — `atomic_write_bytes` / `_fsync_fd` / `fsync_dir` / (see "Atomic write publication failures" below)
- `src/mind_meld/storage/local.py` — `LocalBackend.put` / `_needs_fsync`
- `src/mind_meld/lockedjson.py` — `locked_json_durable_rmw` / (see "Atomic write publication failures") / `locked_json_rmw` / `locked_json_snapshot` / (see "Shared JSON locking")
- `src/mind_meld/storage/keys.py` — `manifest_key` / `blob_key` / `data_prefix` / `device_key` / `parse_blob_key` / (see "Validated storage keys")
- `src/mind_meld/projectid.py` / `src/mind_meld/manifest.py` — `manifest_to_wire` / `manifest_from_wire` (see "Project identity on the wire (Track 90A)")
- `src/mind_meld/cli.py` — `_do_gc` / (see "Own-device blob collection (Track 71A)"; also read init-devices.md)
- `src/mind_meld/cli.py` — `autopull` / `autopush` / `_auto_command_setup` / (see "Visible failures in automatic commands")
- `src/mind_meld/host_usage.py` — `read_cursor_usage` / `configure_cursor_hook`
- `src/mind_meld/cli.py` — `_quarantine_corrupt_manifest` / (also read init-devices.md)
- `src/mind_meld/initflow.py` — `_register_and_save` / (also read init-devices.md)
- `src/mind_meld/resolveflow.py` — `_ensure_inversion_marker` / (also read conflicts.md)
- `src/mind_meld/attemptlog.py` — `write` / (also read events-retro.md)
- `src/mind_meld/manifest.py` — `walk_generic_source` / `walk_muse_source` / `load_manifest` / `collect_tombstones` / `generate_tombstones` / `marker_skip_globs` / `BROWSER_PROFILE_MARKER`
- `src/mind_meld/manifest.py` — `nested_repo_root` / `nested_repo_skip_prefixes` / `nested_repo_roots_for_paths`
- `src/mind_meld/core.py` — `_prove_omitted_paths_absent`
- `src/mind_meld/cli.py` — `_warn_push_growth`
- `src/mind_meld/core.py` — `_freeze_nested_checkout_entries`
- `src/mind_meld/cli.py` — `_drop_unfrozen_checkout_files`
- `src/mind_meld/core.py` — `_incoming_nested_roots`
- `src/mind_meld/core.py` — `_filter_excluded_paths`
- `src/mind_meld/config.py` — `save_config` / `patch_config_on_disk` / `load_config` / the config.toml keys `exclude_patterns`, `disabled_sources`, `seen_sources` (TOML keys, not module symbols) and their consumer paths
- `src/mind_meld/seen_sources.py`
- `src/mind_meld/sidecar.py`
- `src/mind_meld/pullhistory.py`
- `src/mind_meld/statedb.py` / `src/mind_meld/cli.py` — `log_cmd` / `recapture` (history scope) / (see "Pull/push history log (Track 73B)")

Tests pinning the invariants below: `tests/test_integration.py::TestGCSafety` (own-device collection), `tests/test_integration.py::TestExcludePatterns5C`, `tests/test_integration.py::TestDisabledSourcesTombstoneSuppression`, `tests/test_integration.py::TestCompleteSnapshots`, `tests/test_case_collision.py`, `tests/test_recover.py`, `tests/test_recovery.py`, `tests/test_pullhistory.py`, `tests/test_statedb.py`, `tests/test_seen_sources.py`, `tests/test_manifest_fuzz.py`.

---


## Attended terminal outcome (Track 78A)

For non-preview push/pull, the outer `_sync_outcome` owns one final plain line:
success on stdout; failed/incomplete/interrupted/refused on stderr with one
existing fixing/diagnostic command. Default and verbose share its grammar.
Flush stdout and stderr before that write, then flush its stream. Delivery to
a broken pipe is not guaranteed; reporting failure never retries sync or undoes
known effects. Auth/migration/conflict prompts and read-only previews keep their
existing behavior. No global stream interception is used.

```text
setup/auth -> mm lock -> unchanged core / publication ledger
                        -> invocation-local accepted or recovered result
           -> capture receipt -> unlock -> eligible update
           -> history command_scope notice -> attended run record/notice
           -> flush streams -> ONE outcome -> explicit exit
```

Push hands its result off immediately after manifest acceptance, before
fallible reporting/maintenance. Pull hands off its recovered result after
bookkeeping/fsync and before optional summary/rethrow. Observers only store
facts; there is no rescan, replay or rollback. Core unexpected/interrupt objects
still propagate unchanged. The outer attended boundary translates them to exit
1 after cleanup; it emits no Click Abort/default traceback afterward. A compact
trusted-frame trace is verbose-only (class, safe errno, basename/function/line;
no messages, source lines, locals, paths or cause messages).

Push display counts are recomputed from accepted user-file diffs after upload
holds. Existing content_files/content_changed evidence and GC semantics stay
unchanged; invocation-only published_content_files and metadata/selection/repair
facts distinguish accepted bytes, metadata, selection and repair. Usage is
reported only with publication proof, never counted as user files. Held counts
stay separate; paths/reasons are verbose. Pull uses total_applied with conflict
copies included and distinct skipped/failed counts, across the selected scope,
not as unique paths. No peers/no sources are explicit branch facts.

A positive failed-file count ends attended pull with
`Pull incomplete [result-degraded]` and explicit exit 1 **after** writing its
existing degraded run record. Autopull retains exit 0, per-file warnings and its
`N file(s) failed` breadcrumb. Conflict-fail preflight keeps exit 3 before writes.
Original command failure wins over optional finalizer/output failures; accepted
or applied effects remain visible. Unknown effects direct read-only diagnosis
before retry; no blind retry advice or repair guarantee is added.

`_sync_failure` keeps curated refusal text intact, including any alternative
remedy it names (such as `mm disable-source <name>`), and drops only a trailing
`then run <cmd>` step that repeats the selected `Run:` command: `mm update`
(newer/unsupported format), `mm migrate-config`, `mm init`, `mm push` (snapshot
refusal), else `mm diag`. Conflict-fail preflight selects
`mm pull --conflict-mode keep-both`, or `mm diag` for predicted local failures. Uncaught `MindMeldError` text is kept; other
unexpected errors show only their class name. Setup, authentication, lock-held
and refused stops, and setup/crypto-session errors without a returned result,
report no content published / no files applied. Counts are singular at 1
(`1 file applied`, `1 content change published`). The `MINDMELD_PASSPHRASE`
remedy replaces a passphrase error only when stdin is not a terminal. A
no-source attended push ends `Push: no enabled sources.` and records run cause
`no-changes`, not `ok`.

Presentation never changes quiet/attended execution, budgets, warming, locks,
encryption or schemas. In a real terminal (`not verbose and not dry_run and console.is_terminal and console.file.isatty()`), attended `mm push` and `mm pull` render a transient Rich spinner (`_SyncProgress`) with phase labels and measured batch counters (`updateprogress.UpdateProgress`; pull counts per source batch), animated by a 100 ms daemon ticker thread that joins with a bounded timeout on shutdown (`_stop_sync_progress`), repainting per-file advances at ticker rate (phase changes and first/final files paint at once; synchronous refreshes can still wait on terminal output), pauses around mid-run warnings or interactive conflict prompts on the push/pull paths (`_pause_sync_progress`; leaf-module warnings cannot pause it), and clears before the final outcome line. Progress/capture/cache/budget/nested notices are verbose;
actual degradations reach existing status/diag evidence, including failures
without a returned result. Actionable/data-at-risk warnings remain visible in
all modes before the outcome: growth, migration, missing custom events root,
unsafe/corrupt peer skips, kept conflict copies, apply/fsync/recovery failures,
unavailable history/run evidence and actionable update faults. Automatic successful changes and no-ops
are silent; existing automatic error/exit policies remain. The emitter census
and isolated transcript are implementation artifacts, not live fleet acceptance.


## Project identity on the wire (Track 90A)

Claude keeps a project's memory in `~/.claude/projects/<folder>/`, where
`<folder>` is the checkout's path with every non-alphanumeric character replaced
by `-`. That name is machine-specific, so the same repository checked out at two
paths, or on a Mac and a Linux box, was two projects. `projectid.py` (a leaf)
and the codec in `manifest.py` publish a machine-independent name instead.

**One codec, at the byte boundary.** In memory every manifest carries **local**
folder names: walkers, tombstones, diffs, holds, freezes, uploads, apply,
previews, status, history, `exclude_patterns`, `synclog` and the sidecar never
see a wire name. `core.encode_manifest(local_manifest, *, wire_names, ...)` is
the one production path to published bytes: it maps through
`manifest.manifest_to_wire` (a copy, never a mutation) and stamps
`"wire_names": true` and `wire_folders` on the local `claude` entry in place, so
the sidecar keeps them. `load_manifest` ends with `manifest_from_wire`, once per
decoded manifest, requiring the marker `is True` and never raising on shape. The
sidecar is read through `normalize_manifest`, not `load_manifest`: it is already
local. Only the source *named* `claude` is mapped; a renamed claude-type source
keeps machine-specific names. `ProjectFormState` (`core.project_form_state`) is
the one decision for push, its dry run and `mm status`, and reads no device record.

**Names.** For a `projects/<folder>/...` key: (1) the folder owns an identity,
`@git-<quote(identity, safe="")>`; (2) it equals the encoded home, `@home`;
(3) it sits under it, `@home-<rest>`; (4) otherwise literal. No encoded name
contains `@`, and percent-encoding removes `/` and `:`, so a wire name is one
path segment that `_validate_rel_path` accepts (a hostile remote with `..`
stays inside that segment). The identity is `projectid.project_identity`: the
canonical `host/path` of a network `origin` (`https`, `http`, `git`, `ssh`, or
scp with user `git` or none) with a non-default port kept (`host:port/path`),
another ssh user kept (`user@host/path`, with a leading `/` kept, so a path
relative to that user's home differs from an absolute one) and no password, ever.
A query or parameters on the last path segment are dropped; `?` or `;` in an earlier
segment means no identity. Local paths, `file://`, a loopback or unspecified host, a
remote helper (`gcrypt::`), host-only forms, control characters and a malformed URL
or port have none. This
name authorizes writes and deletions, so it is stricter than `retro-fleet`'s
`canonicalize_remote_url`, which still groups loosely.

**The index.** Built lazily, once per process, over every directory under the
`claude` source's `projects/`, on a daemon worker with a 2 s wall-clock cap
(about 60 ms measured at 361 folders). A folder owns an identity when decoding
its name by descent from the filesystem root (never following a symlink, with a
per-run listing memo) gives exactly one directory, that directory has a `.git`
directory with `HEAD`, and its `.git/config` (`RawConfigParser`; a quoted or
escaped value, an inline comment or continued line, a section spelling the parser
misses, an `include` or a parse failure asks git itself about this file only, with a
2 s timeout, so it never reads a parent repository's config) names
a network `origin`. No git spawn on the common path, no transcript read, no walk
of the home folder. `FileNotFoundError` and `NotADirectoryError` mean gone; any
other failure, including a non-regular `.git/config`, and a timeout, mean
*indeterminate*: the folder keeps the identity recorded for it in this machine's
sidecar (`wire_folders`, injected by `core._configure_project_names` because
`sidecar` imports `manifest`, and read only when the index is built), so a stale
mount or a hook with narrower disk permissions never flips a folder to
home-relative. One odd folder never stops the others. Each identity has one owner,
and a folder owns one identity: folders holding `memory/` first, then the smallest
encoded name; a retained identity competes only when nothing resolved; next, a
folder deleted from `projects/` whose recorded checkout still resolves to that
identity, so deleting a project's folder is a real deletion; a linked worktree's
nomination of its main checkout, and the current directory's main checkout (pull
paths only, `projectid.allow_cwd_candidate()` at `_pull_core` entry, probed on its
own daemon thread under the same deadline), join only when the identity has no
other candidate. Non-owners publish home-relative. Linked
worktrees own nothing (Claude writes to the main checkout's folder); a
submodule, a bare layout, no `origin` and a local-path remote are home-relative.
`mm diag --json` reports the index as `project_index`.

**The first switch.** A sidecar from before it records no identities, so `Index.record_identity`
resolves the folders it names that are gone from `projects/`. Their deletion records
publish under the project's identity, with no owner and no `wire_folders` entry, so a later
pull in that checkout honours them; a pull elsewhere changes nothing. A marked sidecar never
does this, and neither does an identity some folder already owns.

**Loading.** `@git-<x>` goes to the folder owning that identity, else stays
literal and is **unplaced**. `@home` forms go to the encoded home unless that
folder owns an identity (then they stay literal and are dropped as
*superseded*: an identity-owned folder takes only identity names, so a stale
copy can neither union into its memory nor delete from it). A literal that names
an owned folder, or any entry whose local name another entry already holds,
becomes `@lit-<folder>`: displaced entries move and never drop, so `mm gc`
keeps every sha. An unmarked entry (a peer still on path names) whose
`base_path` ends in `.claude` under a different home: a folder equal to or under
the publisher's encoded home becomes `@old-<folder>`. Tombstone keys follow the
same rules; one whose source has no entry passes through both ways, except that an
`@` name used only by deletion records (published with the source disabled or the
codec off) maps by the marked rules in any entry, because it names its own
provenance.
`projectid.unplaceable_reason(folder)` reads the name only: `@git-` unplaced,
`@old-` old form, `@home` / `@lit-` superseded. `core._filter_excluded_paths`
drops those in files and tombstones of `claude` at its existing pull, push, diff
and status sites, including on its fast path, so such an entry is never applied,
never tombstoned and never reaches the omission proof. The one exception is push's
own prior (`own_prior=True`), which keeps its deletion records under `@git-` and
`@home` names: the owner or home can come back, and dropping the record would undo
the deletion on the next pull. Pull writes one
`excluded` history row per peer per run for them, and leaves that peer's conflict copies
in place, because one may be the only manifest naming memory it did not apply and that
peer's `mm gc` reads its manifests. `walk_claude_source` skips a
project directory starting with `@`.

**A moved checkout mints nothing.** A publisher records
`wire_folders: {"@git-<x>": "<its local folder>"}`. On load, when the recorded
folder differs from the identity's current owner here, the owner joins an
in-memory `moved_folders` (kept by `_merge_manifests`, never serialized and
stripped if a manifest carries one). `core.reconcile_recovered_prior` does the
same for a recovered prior, adding the recorded folder too, because a sidecar's
files sit under it, and doing so when the identity has no owner now. A marked
entry published from another Claude folder (`base_path` differs: a peer, or this
machine before its home or `claude` path changed) adds every local folder it maps
to, so moving the Claude folder mints nothing either, and so does an unmarked own
prior from another Claude folder. A `projects/` that cannot be listed does the same
for every name form and every kind of prior, for the folders that hold files: an
outage is not a mass deletion. A deleted folder's recorded owner still stands in an
outage, so a pull restores what peers hold and can deliver. Recovery respells a sidecar's
deletion records the way a healthy load would.
`generate_tombstones` skips paths under it, and `core.content_counts` /
`iter_content_diffs` do not count a prior entry under a moved or unplaceable
name as deleted, or a local file with the same sha and subpath as new. A remote
change therefore republishes once (`Push: project names updated.`) and mints
nothing; `republish_due` refreshes `wire_folders` on a push with no other change.

**Unplaced memory** stays encrypted in its publisher's `data/<id>/`, referenced
by its manifest; nothing is written locally. `mm status` prints
`Not placed here: N files in M projects ...` (once per `(identity, rel path)`
across peers, from `core.project_name_report`), `mm pull --dry-run --verbose`
lists them with a clone hint, and a join adds the line once. It is not
`waiting`, not `degraded`, changes no exit code and prints nothing on push or a
default pull. Unplaced and old-form counts are not "pending" for O3.2's 24 h
warning; Track 92A owns honouring that. A peer still on path names shows as
`PullResult.old_form_peers` (`312 files from Mac B wait until it publishes
shared names (mm status)`), kept apart from `WaitingPeer` because it waits on a
person. A marked peer's stale 0x02 conflict copy adds no such row: that machine
has switched, and no remedy on it would clear the copy.

**The first push.** `switch_due` (codec on, own prior not marked) and
`republish_due` join push's no-op check with their own wording
(`Would refresh manifest (shared project names)`), upload no content blob outside
`mm-events`, mint no tombstone, are not content changes and run no GC. The marker
is set exactly when the envelope byte is 0x03. A machine with no readable prior
publishes the new form at its next push and mints nothing; one with no resolvable
home publishes the old form. Rollback past the switch is unsupported.

**Limits (accepted, owner-visible).** Linux's folder encoding is unmeasured
(L1 in the plan); a long name Claude might shorten and a non-BMP character
(Node encodes it as two UTF-16 units) are unverified; a fork, an SSH alias host,
a different path case, or `ssh://` and `https://` on a non-default port is a
different identity, visible as not placed on both sides; a checkout that is gone
or on an unmounted disk flips that folder to home-relative until it returns
(a peer on another layout may keep a duplicate home-relative copy that its
identity-owned folders ignore); deleting a folder whose checkout is gone mints
no tombstone, so peers keep their copies; a stale copy of a project under a path
identity-owned elsewhere stays local and harmless; a literal outside-home name can
reach an identity-owned folder at the same path; folder names decode lossily, so a
deleted `~/my_proj`'s folder resolves to a live `~/my-proj` and publishes under its
identity (two live candidates are ambiguous and own nothing); the current-directory
rule both places a project's memory in, and later publishes memory from, any
directory whose `.git/config` claims that origin, unattended through `mm autopull`
(decided in the plan); an ssh user name is kept as written, so a token used as one
reaches the sidecar and `mm diag --json`; a query string is dropped, so two
repositories differing only by it share an identity; a project folder deleted during
a push that cannot read its checkout, or whose index runs out of time, mints nothing,
so peers keep their copies (a false tombstone would block the project fleet-wide); a
first-switch deletion record binds whatever origin sits at that path then, so a path
re-cloned from another repository inside the 30 days can suppress that project's same-named
file until the record expires; a peer with a withheld name keeps all its conflict copies
until its next publishing push, so a dormant peer's copy can outlive a deletion's 30 days; a
push while `projects/` is missing publishes those folders' removal, so a file deleted
just before it mints nothing and can return, and an identity folder with no deletion
record then restores only on a pull inside its checkout; if no push ran during the outage, once `projects/`
exists again, by a pull or by Claude, whatever is still absent reads as deleted at the
next push, including a peer's blob not yet delivered, as every earlier release treated
any absence; own
manifest recovery combined with a home rename or a folder gaining a remote can leave
one deletion record under the old spelling; a hand-edited `.git/config`
with two spellings of `origin` can read differently from git; `mm diff` lists a renamed project's files
as `+ push` where `mm status` reports only the rename; `mm status` and `mm diag`
read each peer's whole manifest with no time bound, so an iCloud placeholder can
delay them; 14 of the 17 no-remote folders measured were dead paths;
`autoMemoryDirectory` is unsupported.

## Shared JSON locking

`lockedjson.py` (v0.11.14, extended v0.12.22) — extracted single-file flock R/M/W primitive shared by `upgrade.py`, `token_usage.py`, and `identity.py` (v0.11.17). Its read-only shared-lock snapshot serves dry-run planners without creating, rewriting, re-permissioning, or normalizing a cache; R/M/W remains the exclusive mutation path. `locked_json_snapshot` (v0.14.11) takes an optional `blocking=False` so a caller (Grok's diag read) can report contention as unknown immediately instead of waiting; existing planners keep the blocking default. Three contention modes: `block` / `raise` / `warn`. Do NOT route new flock-guarded JSON caches through ad-hoc fcntl calls; extend `lockedjson` if the contract needs to grow. `devices-write.lock` stays ad-hoc — its multi-file lock-on-sibling shape doesn't fit the single-file R/M/W contract.

## Validated storage keys

Storage keys are constructed via helpers in `storage/keys.py` (`manifest_key`, `blob_key`, `data_prefix`, `device_key`, `parse_blob_key`), which validate components at construction time, so a corrupt or malicious peer manifest cannot smuggle a `sha256: "../../../etc/passwd"` through `backend.get`. `data_prefix(device_id)` validates the owner and includes the trailing slash, preventing prefix-sharing identities from entering an own-device listing. Do NOT build storage keys with raw f-strings at new call sites.

## Own-device blob collection (Track 71A)

Both explicit `mm gc` and post-publication GC on a content-changing attended
push use `_do_gc`. It reads only this device's raw, normalized manifest, retaining
excluded, disabled-source and frozen nested-checkout references. It lists only
`storage.keys.data_prefix(device_id)` and requires every parsed blob owner to
equal that device before counting or deleting an orphan. Malformed keys are
preserved with sanitized notices. Peer registry entries and manifests neither
authorize nor veto collection; no peer namespace is collected.

A missing own manifest returns zero without listing or deleting blob candidates,
with a visible warning naming this device, skipped collection and no blobs
deleted. Wait for sync, inspect with `mm diag`, then retry GC; inspection does not
repair missing metadata. No blob completion/orphan-success summary is printed.
A valid empty manifest is distinct and can collect own orphans. Own corrupt or
newer-format metadata refuses in apply and preview. So does an own manifest held
only by conflict copies (iCloud, Dropbox or Syncthing) because the canonical is absent or unreadable
(`ManifestFetch.conflict_copies_only`): an older copy alone is an incomplete
reference set, and no peer reference masks it any more. The refusal's `mm push`
remedy works without a content change: push treats that state like a corrupt
manifest for its no-op gate, rewrites the canonical (keeping the local
sidecar's deletion records) and removes the conflict copies. The pre-delete recheck of
the own manifest runs before candidate listing/deletion; it refuses on the same
conditions, skips like a missing manifest if the manifest vanished, and adds any
references that appeared since the first read. The shared crypto-init gate, caller error
classifications, local retention and own temporary-file sweep are unchanged;
missing-own skips still let the outer local reapers and dry-run completion run.

Before the first attended `mm push` or `mm gc` on any Mac, including a dormant
one, every `mm` it can run must have own-device GC: older releases' GC can delete
peer blobs during a push or gc. Hooks do not run GC, but their background update
is conditional (pipx `@latest`, v1.3.0+, `auto_install`), so it is not a rollout
guarantee. This device never cleans abandoned peer
blobs, and 1.x has no retirement command. A lagging peer reading a prior manifest
can still miss an own blob that has since been collected. Protection is limited
to peer namespaces from this updated installation's GC; clone/shared-device-ID
protection and general arrival-order safety remain outside this repair.
`mm update` covers only pipx installs: any other `mm` on `PATH` (Homebrew, an
old worktree build) that predates own-device GC still runs the fleet-wide
collector on attended push or gc, so the README Upgrading note asks for a
per-Mac `which -a mm` check and treats "this device's" in `<path> gc --help` as
the marker of an own-device build. `autopush`/`autopull` do not run GC. Before Track 71A any Mac's attended content
push swept every namespace; now a Mac that syncs mostly through hooks keeps its
superseded blobs until an attended content push or `mm gc` runs on that Mac.
Adding own-device GC to autopush or a status nudge is deferred work.

## Visible failures in automatic commands

`ConfigError` (bad `config.toml`) surfaces as a one-line stderr message — not a silent exit. This is the visible-failure contract: truly unexpected errors still degrade silently via the generic `except Exception` fallback, but malformed config is loud so users don't wedge their background sync without noticing. Relies on `load_config` normalizing non-`ConfigError` exceptions (e.g. cyclic-symlink `.resolve()` failures) into `ConfigError` at the load boundary — do not bypass that by calling `_validate` / `_apply_defaults` directly from a new call site.

**Load-bearing warnings reach stderr even in quiet mode (v0.8.1).** The visible-failure contract extends beyond `ConfigError` to a curated set of degradation signals that quiet-mode used to swallow: corrupt-manifest sidecar recovery, corrupt-manifest peer-fallback recovery, "no sync sources" misconfig in autopush, durability `fsync_dir` failure on pull, and per-file apply failures. Apply failures print one `mm: warning:` line per failed file plus the count line. Do NOT add a new `if not quiet:` gate around a warning that signals data-at-risk degradation — match the established pattern (always-stderr, prefixed `mm:`).

**`autopush` writes a `no-sources` breadcrumb (v0.8.1)** when `get_sources(config)` returns empty, distinguishing "broken config no-op" from "nothing to push" no-op. Without this, `mm status` only sees `outcome: "success"` forever and monitoring on top of it never catches the wedge.

**Keyring skips (Track 73A).** `_auto_command_setup` catches `KeyringUnavailableError` before `CryptoError`: one `mm: <verb> skipped - <remedy>` stderr line in every mode (same shape as `no-passphrase`), exit 0, no `auto<verb>.log` entry, and a `keyring-locked` breadcrumb (detail `locked` or `denied`) or `keyring-no-collection` (no detail). `mm status` prints a post-hoc hint after the `Last auto-<verb>` line for both outcomes, with or without a detail. The reader contract — why the keyring cannot answer, the chainer walk, env fallback and residuals — lives in [init-devices.md "Non-interactive keyring reads (Track 73A)"](init-devices.md#non-interactive-keyring-reads-track-73a). The pre-existing `keyring-error` arm (unexpected exception kinds) and `no-passphrase` outcome name are unchanged; the no-passphrase message now names the backend after its pinned prefix, and its breadcrumb detail records the module-qualified backend the hook process saw (`crypto._keyring_backend_label()`; readers: `mm status`, `mm diag` `last_autorun`), because hooks run in the agent's environment, which a terminal `mm diag` may not share.

## Atomic write publication failures

[`fsutil.py`](../../src/mind_meld/fsutil.py)'s `atomic_write_bytes` publishes at
successful `os.replace`. Before replacement, this invocation preserves an
existing target's bytes or leaves an absent target absent, assuming no external
writer. Replacement changes the pathname, including replacing a symlink rather
than writing its referent; default mode lookup uses `stat`, which follows that
referent. With `fsync=True`, directory open/flush/close runs after replacement
and can fail while complete new bytes are visible and durability is unconfirmed.
Cleanup never rolls back publication. A caller must follow its own stop/readback
policy before deciding what to write next; a failed durable return alone does not
establish the disk state. With `fsync=False` no fallible step follows
`os.replace`, so an `OSError` / `StorageError` raised by the call means this
invocation did not replace the target (an asynchronous interrupt in the last
bytecodes is not covered). Pull's apply sites rely on that: a raised write
becomes `reporter.failed(...)`, which neither voids a deferred keep-local bump
nor registers a touched parent. Moving an apply site to `fsync=True` first needs
a publication signal from the primitive (see "Residual windows" below).

`fsync=False` is the default and requests no fsync calls or crash persistence.
`fsync=True` flushes the temp file before replacement and the parent afterward;
normal return confirms those platform-reported local flushes. `_fsync_fd`
prefers Darwin `F_FULLFSYNC`, falls back to `os.fsync` on ENOTSUP/EINVAL/EOPNOTSUPP,
and uses `os.fsync` directly elsewhere. Real I/O errors propagate. This is no
physical power-loss, iCloud upload or peer-delivery certification. Readback
establishes visible state only; a later flush does not retroactively establish
the failed operation's guarantee. `fsync_dir` alone does not flush file contents.

`errors.py:StorageError` subclasses `MindMeldError`, not `OSError`.
`atomic_write_bytes` wraps `OSError` inside its temp-write/replacement try, but
mode/stat errors other than FileNotFoundError occur before that try and escape
raw. `fsync_dir` wraps open/flush errors as `StorageError`, which propagates;
its finally-close can raise raw `OSError`, mask a flush error, and be wrapped by
`atomic_write_bytes` **after publication**. Neither type nor message prefix is a
publication-phase API. Only caught `OSError` triggers best-effort unlink of the
owned temp. Unlink failure, process death or other uncaught exceptions can strand
it. `retention.py:_sweep_local_tmp_files` covers only this device's storage
`data/<id>/` and `manifests/<id>/`; no reaper covers other temps (devices,
crypto-init, config, caches, markers, the skill store, or pull/resolve temps in
source trees, which the manifest walker's `*.tmp` exclusion keeps out of sync).

**Point-in-time inventory.** Re-enumerated on 2026-10-04 at HEAD
`e9e56dbe804dd2a109ebb0dc5c5ddc66cc53f583`: search `src/` with
`rg -n 'atomic_write_bytes|locked_json_durable_rmw' src`, resolve imports/aliases
and every call's omitted/literal/conditional `fsync`, then search `backend.put`,
`save_config` and `patch_config_on_disk` and follow their consumers, handlers and
next readers. An ad hoc AST call listing cross-checked the source trace: 19 helper
calls, six literal True, one conditional, eight False, four omitted (default
False). The seven durable writer families below (the six True calls plus the
conditional `LocalBackend.put`, which splits by key prefix into the crypto,
manifest and device rows, so nine rows) cover all currently found durable calls;
seven is an observed count, not an allowlist or an automated inventory gate.
`host_usage.py:configure_cursor_hook` (v1.5.0) adds a second
`locked_json_durable_rmw` call after this inventory; the counts above are not
recomputed, and its row below carries no verdict.
Re-enumerate when changing a writer or wrapper. Verdicts qualify the inspected
policy, not physical crash survival or every CLI retry.

| Durable writer and consumers | Error policy and next reader | Evidence verdict |
|---|---|---|
| `lockedjson.py:locked_json_durable_rmw` → `host_usage.py:read_cursor_usage` | The sibling lock spans read and atomic replacement; errors propagate through context exit/finally unlocking. Cursor catches `OSError` / `StorageError`, returns incomplete `io_error`, and publishes no successful observation. Its next invocation opens actual history again under that lock; `cursor_usage_diag` uses `locked_json_snapshot`. Pruned runs make this authoritative history. A taken standalone-completion batch (`cursor-standalone-spool.jsonl.merging`) is unlinked only after this write returns, so a failed write re-folds it idempotently on the next read. | **No demonstrated defect:** source shows fresh reads and no stale retry; `tests/test_lockedjson.py:TestDurableJson67A` pins exclusion/coherent snapshots. Cursor's existing file-flush test does not qualify its post-replacement CLI outcome. |
| `lockedjson.py:locked_json_durable_rmw` → `host_usage.py:configure_cursor_hook` (`~/.cursor/hooks.json`) | Same sibling-lock primitive. No write when the file already has the wanted state, and a symlinked file is refused before the primitive runs. `cli.py:_toggle_cursor_usage` catches `OSError` / `ValueError` / `RuntimeError` / `StorageError`: enable stops with an error before it writes consent, while disable has already revoked consent and only warns. Each re-run re-reads the actual file under the lock; `cursor_hook_state` reads it without a lock. | **Unassessed:** added after the inventory. Tests pin the symlink, wrong-version and malformed-JSON refusals; none injects a replacement or directory-flush fault. |
| `storage/local.py:LocalBackend.put_exclusive` → `crypto.py:bootstrap_crypto_init` (the key header, `mm-crypto-init`) | Its only caller is `initflow._bootstrap_new_fleet`. A write or fsync failure attempts to remove `put_exclusive`'s own temp file and raises; a lost race (`EEXIST`, or a header already present) refuses and names `mm init`; any other failure exits 1 saying no key header was created. `crypto.py:apply_crypto_init_repair` no longer writes: it only unlinks conflict copies byte-identical to canonical. Next `fetch_crypto_init` reads actual canonical/copies and creates a fresh plan. | **No demonstrated defect:** the exclusive create is atomic locally; two fleets minted at once on unsynced machines remain possible and are caught afterwards by the fingerprint refusal, which deletes nothing. See init-devices.md. |
| Same `LocalBackend.put` → `cli.py:_push_core` (`manifests/`) | Put failure stops before `content_accepted`, sidecar, last_seen and conflict cleanup. `push` catches `OSError` / `MindMeldError`; `_auto_command_scope` records failure for autopush. Push's finally then records any attended capture attempt as `push-failed` via `Attempt.stopped()`, and `_host_publication` judges publication from the unrefreshed sidecar. Next push/pull uses `_fetch_remote_manifest` and `_recover_prior_manifest` on actual storage; a following no-op push returns before `sidecar.write`, so the sidecar can stay one generation behind a visible manifest and later corrupt-manifest recovery would use it as prior state. | **Unknown:** no stale content retry was found, but after a late error that follows visible replacement, `push` says content was not pushed, the attempt record says `push-failed`, and `mm status` reports publication `unknown` for a manifest peers can read. Owner: CLI/sync maintainer; prove pre/post-failure receipt, warning, attempt/status and next-push sidecar behavior with isolated manifest-parent faults before proposing a repair. |
| Same `LocalBackend.put` → `devices.py:update_last_seen` (`devices/`) | Put errors propagate after the read inside `_devices_write_lock`, which yields without the lock once its retries are exhausted (its warning says the update is skipped; it is not). `_push_core` warns/continues for attended maintenance; unattended errors reach `_auto_command_scope`. Next `update_last_seen` re-GETs; `list_devices` / `list_devices_with_drops` read current registry bytes. Registration itself uses the excluded create-only writer below. | **No demonstrated defect** in publication-failure handling: current last_seen/version fields are recomputed from a fresh read, with no cached-state retry. Under lock contention the read-modify-write can run unlocked. `tests/test_storage_local.py:TestFsyncRouting` pins the conditional prefixes and mode, not every failure handler. |
| `sidecar.py:write` → `cli.py:_push_core` | Catches `OSError` / `StorageError`, warns and continues after successful manifest acceptance. It does not restore the old sidecar. Next `_recover_prior_manifest`, `_host_publication`, `_collect_diag_state` or `recover` calls `sidecar.read`, which opens current bytes and validates shape/device identity. | **No demonstrated defect** in the inspected read/write policy. The warning's promised peer fallback overstates what a late error proves: a visible valid sidecar is still eligible. No fresh reproduction of historical sidecar disappearance is established. |
| `config.py:save_config` → `initflow.py:_register_and_save`; `config.py:patch_config_on_disk` → CLI patch consumers | Save errors propagate. Init's `except Exception` deletes the device key and re-raises without config readback. Patches first re-read TOML: `_set_grok_host_usage`, `disable_source`, `enable_source`, `reconfigure_sources`, `_migrate_config_core` propagate write errors and stop before success/acknowledgment; migration releases its lock in finally. `install_skills_cmd` catches `ConfigError` / `OSError` / `StorageError` and exits before changing links; its "could not write" message, like the sidecar warning, can follow a visible write. `_init_crypto_session` backfill catches only `OSError` / `ConfigError`; `StorageError` reaches its command's crypto-error handler. Next `load_config` / `_get_config` and init's `_load_prior_device_metadata` read actual config; each later patch also re-reads. | **Reproduced defects** in init cleanup and in the backfill handler (both below). **No demonstrated defect** in the source-toggle/migration/install stop-and-fresh-read policies. No config-family blanket safety claim. |
| `attemptlog.py:write` → `cli.py:push` guarded finally | A separate `except Exception` notices that the attempt could not be durably recorded; outer finally releases the mm lock. No rollback or altered content-sync exit. Next status/diag uses `attemptlog.project` → `read`, with distinct missing/unreadable/corrupt states. | **No demonstrated defect:** existing `tests/test_integration.py:test_attempt_write_failure_keeps_push_exit_and_releases_lock` covers pre/post-rename and mkdir faults, actual record state and released lock. events-retro.md already admits old/new-record uncertainty. |
| `cli.py:_quarantine_corrupt_manifest` → `recover` | Copy errors propagate before source unlink, so a late copy failure can leave both names. `recover` catches FileNotFoundError / `OSError`, not `StorageError`; the post-unlink directory flush separately catches both. A later recover or push re-fetches the canonical through `_fetch_remote_manifest`; no same-call retry. | **Reproduced defect** in presentation (below): the copy's `StorageError` escapes `recover` uncaught instead of its "quarantine failed" error. Source retention holds; a re-run quarantines again and leaves any earlier copy. |
| `resolveflow.py:_ensure_inversion_marker` → `_migrate_pre_inversion_conflict` | Catches `OSError` / `StorageError` / ValueError and returns None; that migration is skipped. A later call reads the actual marker if present, including one published before a flush error, rather than rewriting a cached timestamp. | **No demonstrated defect:** inspected fail-safe gate and fresh read; conflicts.md supplies filename-era/clock rules. This does not certify physical survival of the marker. |

**Existing separate init reproduction.** At this same HEAD, the planning probe
ran the real `_register_and_save` against temporary config and LocalBackend paths,
with Keychain access forbidden. A config-parent `StorageError` left the new
device id in config.toml but deleted its registry entry; the file-flush control
left config absent and removed registration. Both cases passed. The roadmap card
"Preserve coherent state after config publication failures" (Track 71A in
[ROADMAP.md](../ROADMAP.md), drained from the TODOS entry "Init deletes device
registration after a published config save fails") owns repair under the
init/CLI maintainer. Preserve
pre-publication cleanup and investigate `_ensure_device_registered` self-heal,
full CLI retry and passphrase/guard behavior before choosing a repair. Do not
assume the registration is durable or was reported accurately: its
`put_exclusive` write has no parent flush (see Exclusions), and
`register_device` treats every `StorageError` as an existing entry. Those
outcomes remain unqualified; Track 69B changes no consumer or exception API.

**Review reproductions: handlers that catch `OSError` for a `StorageError`.**
Two handlers expect `OSError` from a helper write that raises `StorageError`.
Both were reproduced on the Track 69B branch, whose runtime matches that HEAD,
with tmp_path state under the repo's conftest isolation, each with a file-flush (pre-publication) and a parent-flush
(post-publication) fault. `_init_crypto_session`'s backfill comment says
`except OSError: pass  # non-fatal`, yet `StorageError` escaped in both phases,
which its callers turn into a command error; after the parent fault the
backfilled keys were already on disk. `mm recover --abandon-manifest --yes`
exited 1 on an uncaught `StorageError` rather than "quarantine failed"; the
source stayed, and after the parent fault a quarantine copy existed too. A
fault-free re-run completed and left that copy. The same roadmap card (Track
71A, drained from the TODOS entry "Two handlers catch OSError for helper writes
that raise StorageError") owns repair.

**Exclusions.** Explicit False calls are `pullflow.py:_apply_write`, `_apply_merge`,
`_apply_conflict`, both writes in `_apply_incoming_file`,
`resolveflow.py:_resolve_interactive_loop`, `seen_sources.py:write` and
`synclog.py:write_sync_log`. Omitted/default-False calls are the two sentinel
writes in `skill_link.py:_prepare_store_dir` and payload/metadata writes in
`_publish_skill_store`. `cli.py:_upload_changed_blobs` selects `data/`, so
`LocalBackend.put` is non-durable there (and for unknown prefixes).
`LocalBackend.put_exclusive` uses its own create-only link protocol, outside this
helper's replacement contract and weaker than its durable prefixes: one
unchecked `os.write`, plain `os.fsync` (not `F_FULLFSYNC`) and no parent flush.
A raw write/flush `OSError` now attempts to unlink its own temp before
propagating; a failed unlink can still strand it. Its consumers are
`devices.py:register_device` and `crypto.py:bootstrap_crypto_init`. Ordinary
`lockedjson.py:locked_json_rmw` / `_write_json` forensic caches (identity,
upgrade, token usage, Codex/Grok/Muse host usage), `seen_sources` in-place flock
writes, `fsutil.py:flock_append_jsonl` and the Cursor completion spool's
`fsutil.py:append_rotatable_jsonl` / `rotate_jsonl` (an fsynced append and a
rename-aside rotation; between them they call `_fsync_fd` and `fsync_dir`
directly, never `atomic_write_bytes`) are separate protocols, not omitted
durable atomic-write families. Pull's end-of-batch `fsync_dir` has the separate
apply exception boundary below. This audit does not qualify the crash
durability of these `fsync=False` writes: a directory flush covers entries, not
file contents, so `_apply_write`'s "Deferred durability" comment and
`storage/local.py`'s "self-healing via re-push" rationale for `data/` blobs under
a durable manifest remain unverified premises.

**Remaining helper unknown.** If `os.fdopen` fails after `mkstemp` returns,
descriptor ownership/closure is not proven by temp-unlink assertions. Owner:
fsutil maintainer; a bounded descriptor-count/fd-validity fault probe must
establish a leak before filing a repair. No such defect is claimed here.
Existing `tests/test_fsutil.py:TestAtomicWriteBytes`, `TestFsyncFd`, `TestFsyncDir`
and `tests/test_memory_contract.py:test_C2_real_atomic_helper_failure_reopens_actual_coherent_bytes`
retain phase/mode/platform and four-fault readback/stale-refusal coverage. C2 is
a tests-only model, not production caller or cloud qualification. Review found
one concrete missing assertion: the fallback test now covers all three
documented `F_FULLFSYNC` errnos, so on Darwin (where they are distinct)
narrowing that tuple fails a test.

## Complete snapshots (load-bearing, v0.14.3)

Publishing scans (`_push_core`, including the mm-events rescan) use strict mode: directory enumeration, classification, descriptor identity, and hash errors raise `SnapshotError` instead of omitting the path. Status also uses strict mode to avoid a falsely healthy comparison; other diagnostic walkers stay permissive. A published entry's digest and size describe the accepted file bytes and mtime describes that same observed revision. This is not a filesystem-wide atomic snapshot.

**One changing file is held, not a refusal (Track 75A).** `SnapshotChanged(SnapshotError)` (`errors.py`, with `reason` `changed`, `vanished` or `grew`) is the only transient snapshot exception. It comes from `read_file_revision` and `_upload_changed_blobs`, classified at the raise site and never by message text. The reader reports changed, vanished or grew past `max_file_size` *while being read*: a file under the cap at its pre-open lstat and over it at the descriptor is `grew`; over it at both is the unchanged `_FileTooLarge` skip. The per-file lstat in the muse and generic loops separately re-probes `FileNotFoundError` once, then records `vanished` directly when a collector is present or raises plain `SnapshotError` without one. Everything else stays a plain `SnapshotError` and refuses the whole push, so a retry that lands in one of them refuses instead of holding: could not be read, directory enumeration (including a discovered directory that vanishes and `.extend-root` marker discovery), confinement (symlink, non-regular file, hardlink privacy), missing previously published roots, and still-present omissions. The subclass keeps every `except SnapshotError` handler, pull's included, unchanged.

Each site re-reads **once**, counted exactly: scan read 1 in `_record_file`, immediate read 2; upload read 1 is the verify read, read 2 is deferred to the end of that source's upload loop (the other uploads are the settle delay; no sleep; there is no read 3). A verified read 2 publishes *its own* revision, which need not equal read 1 or the scan: at upload its blob is keyed by its own digest and `_upload_changed_blobs` returns the replacement entry in `UploadOutcome.patches` (a read-2 digest equal to the scanned one is an idempotent put plus an mtime-only republish, no special case). A file still changing after read 2 is **held** when the caller passed a collector (`build_manifest_v2(held=..., held_candidates=...)`, opt-in like `nested_roots`; status and diff pass none, and a transient without one raises after its retry). A hold leaves the previously published version in place: the file keeps its accepted prior entry (no upload, no tombstone), or, if it was never published, is left out. Only the accepted prior entry is ever carried, never a first-scan revision: that revision's blob is not stored until the upload loop, so carrying it would publish a dangling entry. The same file is therefore never both held and re-uploaded, and a held scan path reads as unchanged and cannot be held again at upload.

Hold eligibility is exactly `not recovering_from_corrupt` (`fetch.status == "corrupt"` or `fetch.conflict_copies_only` or `fetch.vanished`). `ok` holds at the fetched entry; `missing` (first push, not recovery) holds by omission; a recovered prior (sidecar, peers or conflict copies) was never accepted as-is, so there the retry still runs and settles normally, but a still-changing file refuses, naming the file and the fix. `_push_core` passes no collector under recovery and `_upload_changed_blobs(allow_hold=False)` re-raises. Held-file injection (`_inject_held_entries`, one helper for the scan and the rescan) runs after the unselected/exclude filters, the symlink prior-drop and the nested-checkout freeze, and before the omission proof and tombstones: it skips frozen, excluded and tombstoned paths (pruned, never counted: excluded or deleted, not waiting), needs no blob-exists probe (parity with the accepted freeze path), and so needs no omission-proof exemption (the proof skips locally present paths). A hold candidate is a scandir entry that vanished between its listing and its stat; it is re-lstat'ed at injection: still missing and an exact prior file holds at the prior, still missing under a prior directory refuses (an unenumerated subtree cannot be proven tombstone-safe), still missing and neither is omitted, present again in any form refuses.

The mm-events rescan replaces its sources, so it takes their holds from its own scan: a path it reads cleanly leaves the held set and uploads normally, a path it holds keeps its accepted prior entry (or is left out), and a held path under a checkout the rescan reports moves to frozen handling (held and frozen stay disjoint; upload skips both). An upload-time hold puts no blob and skips only that file: the loop continues and the manifest still publishes the rest. Its entry is patched back to the accepted prior entry (or removed) and read-2 patches are applied **before the manifest is serialized**, so the manifest never advertises a blob that was not put. After upload holds, tombstones are reconciled against those final entries: a recreated file that could not publish keeps its accepted deletion and is not counted as waiting. Counters describe published bytes: upload holds leave `total_new`/`total_modified` by fresh-diff membership, and `content_files` only for non-mm-internal sources; `content_changed` is the pre-upload gate fact and is not recomputed.

| State | What you see | What to do |
|---|---|---|
| **Held** (still changing, vanished or grew) | `PushResult.held`; the attended outcome names the count; paths and reasons are verbose-only as *not yet synced*; autopush records them in a `degraded` breadcrumb and prints nothing | Nothing: the prior version stays published and the file goes with a later push. A file held on every push is one something keeps writing; `exclude_patterns` ends it. |
| **Frozen** (under a nested git checkout) | `skipped: nested git repository`; its published entries are kept | See [Nested git checkouts](#nested-git-checkouts-are-frozen-never-published) for resuming sync or excluding the subtree |
| **Skipped / excluded** (oversized at the scan, glob, marker) | `skipped:` lines with `--verbose`; excluded paths silent | Raise `sync.max_file_size` or adjust the glob |
| **Tombstoned** | The deletion record is published; a path with a tombstone is never frozen or held | Nothing: the tombstone suppresses restoration of a missing file; existing peer copies remain |
| **Refused** (could not read, enumeration, confinement, missing root, recovered prior with a changing file) | One four-part `Cannot publish: ...` reason in the final `Push refused [snapshot-refused]` line, exit 1, previous snapshot kept | The fix in the message; blobs uploaded before the stop remain as orphans for GC |

Frozen checkout entries and held files are the **only** sanctioned exceptions to a complete snapshot. A third exception requires revisiting the manifest model, not another special case. Held-only exits: a scan that holds everything takes the existing no-substantive-change exit (no manifest put, sidecar, last_seen or events tail) but returns a `PushResult` carrying `held` and never claims "up to date"; an upload that holds everything still puts the manifest. `mm push --dry-run` applies the scan hold, shows the held count after the per-source summaries, and exits 0; upload re-reads stay unpreviewed. `mm recapture` takes its written-locally-but-not-synced exit when any held path is mm-internal. A file that is written continuously can be held on every push; the held count is visible each time (paths with `--verbose`), and escalation is deferred (docs/TODOS.md).

The autopush breadcrumb budgets about 300 characters for its combined detail.
It prefers whole held paths, truncates a long first path when needed, and counts
omitted paths with `+N more`. Existing events degradations and the held count
are never dropped; if those alone exhaust the budget, they take precedence
over paths and the size target.

Status now uses strict hashing for scan completeness (77A), so it does not
reuse `diagnostic_hash` from the single event-file scan. This may reread event
bytes, an explicit unmeasured I/O tradeoff; it adds no EventScan parsing pass.
`_record_file`'s strict branch never consults the callback. Publication always
performs its own descriptor-bound scan and upload verification.

`generate_tombstones` stays a pure comparison. Before the scan and rescan calls, `_prove_omitted_paths_absent` checks omitted prior paths against the trusted local source map. The final reconciliation after upload holds uses the accepted entries and tombstones described above. ENOENT/ENOTDIR below an accessible root is genuine absence. A still-present regular file omitted by size cap or inode dedup refuses. A missing previously populated selected user-source root refuses the whole push; removing a source from selection drops its file entries, preserves existing tombstones, and is itself a manifest change.

Before applying vanished upload holds for a previously published source, `_push_core`
rechecks its trusted configured root through `_require_published_source_root`, the
same check used by the omission proof. A root that disappears after that earlier
proof still refuses publication; individual missing files under an available root
can be held.

**mm-events ownership (Tracks 59A/60A).** The default `~/.local/share/mind-meld` root is mm-owned: a missing root, a present root with `events/` gone, or individual missing event files represent deletions. A real push creates the default root at 0700 and publishes that snapshot, including its usual new activity row. A missing **custom** mm-events root is never created: push warns with the folder and remedies, drops that source's prior entries without new tombstones, preserves existing tombstones, skips the events tail, and publishes the other sources. Autopush records the same warning as a degraded breadcrumb. When the custom folder returns, the next push publishes it again. User-source missing-root refusal is unchanged.

`_upload_changed_blobs` verifies the upload revision against the scanned digest, size, and mtime **before** `backend.put`. A mismatch, a vanished file or growth past the cap is queued, re-read once after the rest of that source has uploaded, and then either published as its own revision (patched entry) or held (no blob put); it never uploads bytes under a digest they do not match, and a non-transient failure aborts at once. Earlier correctly keyed encrypted blobs may remain as orphans. The encrypted manifest is the commit boundary; last_seen, sidecar, and conflict cleanup run only after that put.

Attended host capture's publication evidence runs immediately after that
manifest put. `PushResult.content_accepted` is recorded at the put, so a later
maintenance error cannot undo content acceptance; `_report_usage_publication`
then reports the row published only when the exact day-file revision containing
it is in the accepted manifest, not merely because the push returned a truthy
`PushResult`. Status/diag use the existing local accepted-manifest sidecar as
evidence; if the current day-file revision no longer matches it, publication
is unknown. A known failed read of the winning revision is `unverified`.
65A's attended receipt hashes the same bytes it parses, once: post-acceptance
mutation is unverified, never proof the row was not published. Configuration
exclusion, absence of the day file from the accepted manifest, or matching accepted
bytes without the row prove non-publication. The private local
`last-attended-capture.json` records that attempt independently; it never goes to
storage, and cannot prove delivery to another Mac. See events-retro.md for its
atomic write, closed vocabulary and write-free inspection contract. Host rows add
only reader-name `empty_sources` evidence, scoped to retained days.

`_download_and_apply` hashes plaintext after the non-mutating symlink/containment guards and before `_apply_incoming_file`. A mismatch is a per-file `failed` outcome; later valid files continue. Unchanged historical blobs are not rewritten by a no-op push; incoming verification rejects them if encountered.

---

## exclude_patterns + consumer-boundary filter (load-bearing, v0.9.1, v0.9.3, v0.11.13)
Per-source `exclude_patterns: list[str]` of fnmatch globs is matched against the
relative path. The default `gstack` source ships with:

- Per-machine caches and state: `config.yaml`, `projects/*/repo-mode.json`,
  `projects/*/land-deploy-confirmed`, `analytics/.last-sync-*`,
  `projects/*/decisions.active.json`, `projects/*/brain-cache/*`,
  `projects/*/pair-review/session.yaml`.
- Generated output (v1.8.0): `projects/*/venv/*`, `projects/*/.venv/*`,
  `projects/*/*.app/*`, `projects/*/.qa-evidence/*`, `*.zip`, `*.tar`, `*.tgz`,
  `*.tar.gz`, `*.tar.xz`, `*.tar.bz2`, `*.tar.zst`, `*.whl`, `*.so`, `*.dylib`,
  `*.pak`, `*.dmg`.

The walker drops excluded paths from the local manifest at push time. The
cache rationale and generator measurements live in
[Generated files are not sync data](#generated-files-are-not-sync-data-load-bearing-v01251).

`_filter_excluded_paths(manifest, exclude_map)` applies at THREE consumer-boundary call sites — all AFTER `_fetch_remote_manifest` returns: (1) `_pull_core` filters peer manifests in `manifest_cache` BEFORE `collect_tombstones` and the per-source download loop; (2) `_push_core` filters the manifest returned by `_recover_prior_manifest` (covers ok / sidecar / peer-fallback uniformly) BEFORE `generate_tombstones`; (3) `diff_cmd` filters its default or `--from` comparison manifest with both exclude globs and marker skip prefixes. A fourth call in `status` filters a copy of the prior to select frozen nested-checkout entries and check omitted paths (see "Nested git checkouts are frozen, never published"); the diff's remote side stays unfiltered. The filter MUST NOT apply at `_fetch_remote_manifest` itself — `mm gc` reads this device's raw manifest via that path to compute referenced blobs, and a filtered manifest there would mark live own blobs as orphans (codex-2 #1, pinned by `test_mm_gc_does_not_orphan_excluded_path_blobs`).

**Tombstone-suppression invariant.** Adding a path to `exclude_patterns` must NOT generate a deletion tombstone on the next push (2026-04-24 first-pull regression). Removing a glob brings the path back as new. Sidecar recovery is filtered too so a corrupt-manifest recovery on a freshly-migrated config doesn't re-introduce pre-exclude paths via the sidecar (codex-2 #2). All four scenarios (two-device first-pull, tombstone-on-exclude, tombstone-on-unexclude, sidecar-bypass-guard) are pinned in `tests/test_integration.py::TestExcludePatterns5C`.

**Visible-failure contract for migration UX (v0.9.1).** Existing configs need to opt in by running `mm migrate-config`. autopull / autopush NEVER auto-mutate config — they record the missing-excludes signal to `~/.config/mind-meld/migration-state.json` and let `mm status` surface it. Interactive `mm pull` / `mm push` prompt-once. Silent config mutation in a hook would be exactly the class of "wedged sync I never noticed" failure the visible-failure contract exists to prevent. Add the new "config missing recommended excludes" warning to the existing curated stderr signal set (corrupt-manifest recovery, fsync failures, no-sources misconfig, etc.).

**v1.8.0 rollout.** Until every active Mac has migrated and pushed, each attended
`mm pull` on a migrated Mac logs one `excluded` row per file an unmigrated peer
still lists under these globs: about 8,000 rows / 2 MB on the measured Mac.
Before Track 73B this exceeded the shared 1 MB cap and evicted earlier
history. The table now retains 20,000 excluded rows independently of conflicts;
legacy backup copies stay readable with jq (not mm log).
[Back up history and upgrade, migrate and push every active Mac before the next
attended pull on any of them](../../README.md#v180-scope-trim).
Auto-update installs per Mac. A default-config Mac publishes the trim on its
next push or autopush; autopush stays silent on success (including those deletions).
Those deletions count manifest entries, remove no local files and write no
tombstones. Autopush never runs GC. An explicit-config Mac waits for
`mm migrate-config`; a direct successful migration tells the owner to run
`mm push` to publish, while migration inside push, pull or recapture leaves
that instruction to the calling command's own flow. `mm status` after pushing
should show no migration warning and no pending gstack deletions.

Optional diagnostic: capture `date -u +%FT%TZ` before an attended pull, then
run `mm log --action excluded --source gstack --limit 0 --format jsonl --since <that>`
and read `device` on rows under the new globs. This identifies peers still
listing those files; it does not certify fleet completion.

## `include_patterns` allowlist + inverse consumer filter

Generic sources may declare `include_patterns: list[str]` (`_validate_sources`
rejects the key on non-`generic` sources so producer and consumer filters cannot
diverge): when non-empty, only relative paths matching at least one pattern are
walked, published or applied. The dialect is segment-aware
(`manifest.matches_include_patterns`): `*` and `?` never cross `/`, a whole `**`
segment crosses (adjacent `**` segments collapse). This is deliberately unlike
the legacy fnmatch exclude globs, whose `*` crosses `/` —
`projects/*/pair-review/*` must mean top-level files only. Excludes win:
global `EXCLUDED`, marker skip prefixes and `exclude_patterns` are evaluated
first in `_is_excluded`, so `pair-review/session.yaml` stays out even though
`projects/*/pair-review/*` matches it. In `walk_generic_source`, `_is_excluded`
also runs before `(st_dev, st_ino)` deduplication so an earlier-sorting
excluded hardlink sibling cannot claim the inode and drop a later-sorting
allowlisted sibling.

**Why.** Measured 2026-10-21 on device 889e42c0: `projects/` held 43,323
synced files / 1.5 GB raw (0.40 GB sealed). Bucketed by first component under
each slug, per-run artifact trees (`review-and-prep/`, `run-*/`,
`*-worktree-tmp`, `gate-diff/`, `defect-candidates/`) were 79% of files and
loose run files another 13%; durable cross-machine context was ~1.5%.
The set grew from 35,689 files (2026-10-13, before Track 72B's generator
globs) to 43,323 after them — blacklist batches cannot outrun producer
vocabulary (1,044 distinct subdir names, 5,656 distinct loose filenames;
names recurring in 3+ projects: 13 dirs, 12 files). The default list in
`config._GSTACK_INCLUDE_PATTERNS` therefore names readers instead:
`/context-restore` (checkpoints + checkpoint-pointers), plan/design resume
(ceo-plans, plans, designs, specs), factory handoffs (briefs, cards,
handoffs, the timestamped `driver-handoff-*.md` archives), land-and-deploy
(deploy-reports), pair-review's `deploy.md`, `report.md` and
`parked-bugs.md`, and the loose ledgers (learnings/decisions search,
timeline + context recovery, question log/preferences/events, taste
profile, `*-reviews.jsonl` reused cross-machine by wtree fingerprint, test
plans `/ship` reads, autoplan plan/restore artifacts). The ten artifact
directories (checkpoints, checkpoint-pointers, ceo-plans, plans, designs,
specs, briefs, cards, handoffs, deploy-reports) admit document types only:
`projects/*/<dir>/**/*.<ext>` for md, txt, html, css, json, jsonl, png, jpg,
jpeg, gif, svg, webp, pdf, patch and diff, matched case-sensitively.
`analytics/**`, `retros/**` and the `include_files` literals keep
non-`projects/` content unchanged. Result on the measured Mac with the
original list: 3,375 files / 40 MB inside `projects/`. New skill output
stays local until an entry is added — fail-closed is the point; admission
requires naming the cross-machine reader. `matches_include_patterns`
compiles the whole list into one cached alternation, so a 184-entry list
costs the walker about what the original 44 did per path.

### Removed 2026-10-29 (Track 94A, owner decisions)

Five readers above were machine-local state, not cross-machine readers, and
left the allowlist. Narrowing writes no tombstones and deletes nothing on any
Mac (mechanics below). Browser profiles under any of these paths are also
skipped by marker (see "Browser profiles (Track 94A)").

- `projects/*/manager-*.json`: mmm-factory manager live state, with agent IDs
  and status/watch files rewritten every poll (gstack sync audit 2026-10-29).
  Factory handover moves to the opt-in `mmm` source (docs/SPEC.md "Factory
  handover").
- `projects/*/*-evidence.jsonl`: gstack-evidence documents itself as
  "MACHINE-LOCAL by design" (`bin/gstack-evidence`). `*-reviews.jsonl` stays.
- the live `projects/*/driver-handoff.md`: the timestamped
  `driver-handoff-*.md` archives stay.
- pair-review top-level files other than `deploy.md`, `report.md` and
  `parked-bugs.md`: the `session.from-*` copies `resolveflow.py` writes and
  stray scripts.
- non-document files in the ten artifact directories (`.sh`, `.cjs`,
  `.stderr`, `.yaml` and similar): per-run scripts and output, e.g. 53 `.sh`
  and 43 `.stderr` files under `plans/` on the measured Mac.

Kept by the same decision: `question-log.jsonl`, `question-events.jsonl`,
`question-preferences.json`, `taste-profile.json` and `analytics/**` still
sync, although upstream gstack keeps them machine-local
(`bin/gstack-artifacts-init`). Hazard: `analytics/skill-usage.jsonl`
set-union merges across Macs, while gstack-telemetry-sync's per-Mac cursor
`analytics/.last-sync-line` (excluded per Mac) counts lines of that file, so
peer lines merged into it shift what the cursor points at and telemetry can
re-send or skip lines.

Review resume survives de-scoping `review-and-prep/` and `ship-and-land/` by
design: both skills keep their durable handoff in the PR receipt and state
that a machine-local path is not portable evidence; the synced
`*-reviews.jsonl` ledgers carry completed-review reuse.
This supersedes [Bulk generator output](#bulk-generator-output) Decision 6's
retention of `compact-snapshots/` / `ranking-summaries/` receipts (owner
decision 2026-10-22): those trees are out with the rest of review-prep.

**Mechanics mirror exclude_patterns.** The walker threads the list through
`_stat_collected_file` / `_record_file` into `_is_excluded`, so a de-scoped
path is silent (no `on_skip`, no hold) exactly like an excluded one; the
walker still scandirs the whole tree (hashing is skipped, traversal is not —
pruning is future work). `_build_include_map` feeds the same
`_filter_excluded_paths` at the same four consumer-boundary sites, stripping
non-matching entries and tombstone keys from prior/peer manifests before
`generate_tombstones` / `collect_tombstones` and the omission proof, so
narrowing mints no tombstones and deletes nothing on any Mac; widening
brings paths back as new. The filter still never runs inside
`_fetch_remote_manifest` (gc-bypass hazard). `_inject_held_entries` and
`_prove_omitted_paths_absent` take the include map for the same reason.
Pinned by `tests/test_integration.py::TestIncludePatternsAllowlist` (narrow,
fresh-pull-from-fat-peer, widen) plus matcher/walker tests in
`tests/test_manifest.py`.

**Migration UX mirrors recommended excludes.** `_compute_recommended_includes_diff`
fires when the key is absent, or when the present list equals as a set a
previous default frozen in `config.RETIRED_DEFAULT_INCLUDE_PATTERNS` (Track
94A: the v1.11.6 list). Any other present list, even empty, is the user's
choice; deleting the key re-prompts. It proposes the default wholesale —
unlike excludes there is no per-glob merge — and `mm migrate-config` shows a
replaced list as `(previous default list)` with only the removed and added
entries. Exclude patterns are left alone, so a local
`projects/*/chrome-profile/*` stopgap stays until the owner deletes it. The signal unions into
`_config_missing_recommended_excludes`, so status breadcrumbs, auto-command
breadcrumbs and the attended prompt all fire unchanged, and
`_migrate_config_core` applies both halves in one config write. Own store
space is reclaimed by an attended content-changing push's GC or `mm gc` once
the slim manifest publishes. Until every active Mac migrates and pushes, an
attended pull on a migrated Mac logs one `excluded` row per de-scoped file
an unmigrated peer still lists — on the measured Mac that is ~40k rows
against the 20,000-row excluded retention, so earlier excluded history
rotates out first; conflict history is unaffected. Migrate and push every
active Mac before the next attended pull on any of them (README "Syncing
gstack").

## Nested git checkouts are frozen, never published

Every source walker prunes a directory with a regular `.git` file or `.git`
directory strictly below its source root, before enumerating or hashing checkout
contents. The source root itself remains exempt. Check ancestors of configured
includes so a direct include inside a checkout cannot bypass the rule.
`nested_repo_root` probes components in order and stops at the first missing,
non-directory or symlinked one, so a checkout above a descendant link is still
found and nothing is probed through a link; callers check it before the
descendant-symlink omission. Strict publishing and status refuse an unreadable component
or checkout-marker probe; other diagnostic scans remain permissive.

**One observation per command.** `build_manifest_v2(nested_roots=...)` returns the
roots its own walk skipped. Push, status and diff never re-walk for them, and
`_build_exclude_map` takes roots only from its caller. Pull (and `mm diff`'s
remote filter) uses `_incoming_nested_roots` / `nested_repo_roots_for_paths`:
it lstats only the local ancestors of incoming peer files and tombstones, once
per directory, stopping at missing, non-directory or symlinked components. Never
reintroduce a local tree walk into pull/autopull. diag's
`nested_repo_skip_prefixes` is the only inventory walk: write-free, best-effort,
not persisted sync state.

**Freeze, do not exclude, on push.** `_freeze_nested_checkout_entries` copies this
Mac's prior entries under each walker-reported root into the local manifest
unchanged, before the symlink filter and without inspecting what replaced them
on disk. The push prior filter leaves those roots' tombstones in place, and a
path with a prior tombstone is never frozen (merged conflict copies can carry
both; reviving it would advertise a reaped blob). Adding `.git` over published
files therefore uploads nothing, mints no tombstones and keeps peer copies;
deleting the checkout later tombstones them like any deletion. Filtering the
prior instead (exclusion) made deletion stop converging: peers kept republishing
their plain copies and the next pull resurrected them. Upload skips every frozen
path, so their changed local bytes are never read. With `fetch.is_ok` the prior
is this Mac's accepted manifest. A recovered prior freezes only entries whose
blob still exists under this device's key; the rest are excluded (their only
loss is convergence on a later delete). `_drop_unfrozen_checkout_files` then
drops every unfrozen prior file entry under a root (tombstoned or blob-less),
keeping tombstones, so the omission guard never probes checkout contents. The
mm-events rescan carries frozen entries across its source replacement, except
paths it sees again (their `.git` vanished mid-push), which upload normally.
The symlink filter then exempts every root (`exempt_roots`), keeping their
entries and tombstones without probing beneath a frozen checkout. Walkers
report a checkout above an absent or linked include too. Status freezes from
the same exclusion-filtered prior so frozen entries never show as pending
deletions, while an excluded one still does.

`_prove_omitted_paths_absent` has no checkout exemption. Frozen entries are
present in the local manifest; anything else still on disk but omitted (for
example an include deselected while its folder became a checkout, unless another
include still reaches that checkout root) refuses as it always did. An
exemption there minted tombstones for files still on disk.

Frozen entries retire when the checkout is deleted (tombstones everywhere), or
without tombstones (peer copies stay) when an `exclude_patterns` glob such as
`<folder>/*` or a new `.extend-root` marker above the checkout covers them. The freeze also ends silently whenever the prior
no longer lists them: disable then enable of the source, `mm recover
--abandon-manifest`, peer-fallback recovery, blob-less recovery, or adding then
removing a glob. Those paths lose delete convergence like the exclusion they
amount to.

Verbose attended push and previews print one `mm: notice: skipped: nested git repository
source:rel` line per root; autopush stays silent (a skip deletes nothing, so it
is not a data-at-risk warning), and status summarizes the selected roots with an exact count and mm diag
for path detail. Diag retains roots. Attended pull (not
`--dry-run` or autopull, which write no `excluded` history) logs one `excluded`
record per root and device manifest, not per file; a peer with only tombstones
there is logged too (informational: pull is additive and never deletes local
files). Pull's probe treats a peer path the filesystem cannot
encode as a stop, never an exception. Conflict-copy discovery (`resolveflow`)
still walks inside checkouts; pull's no-write guarantee covers peer files.
`marker_skip_globs` checks `nested_repo_root` and parent links before its first
lstat of an include, so marker discovery never probes inside a checkout. Status
filters its freeze candidates through the full exclude map, marker prefixes
included: a checkout reached directly by an `include_dirs` or `include_files`
entry can sit below a marker directory, and push drops (never freezes) its prior
entries. Only sanitized
display copies reach terminals; exclusion keys keep their original bytes.

`.git` is a local selection marker like `.extend-root`: `_filter_excluded_paths`
drops any peer path or tombstone with a `.git` segment in any letter case (APFS
lstat matches `.GIT`), so a peer cannot plant one to switch off sync of a
subtree. It also drops `.` and empty segments, which `Path` would normalize away
after literal prefix matching. This is intentionally stricter than push's
case-sensitive `.git/` exclusion; honest walkers never produce these paths below
a source root.

`_warn_push_growth` uses already-materialized new-file diffs, counts groups by
source and first two directory components, and names up to three groups exceeding
1,000 new files. Its warning never refuses a push, includes no modified files and
is skipped without an accepted manifest of this Mac's own (`fetch.is_ok` false).

## Generated files are not sync data (load-bearing, v0.12.51)

**The rule: if a file has a generator on every machine, it does not belong in `exclude_patterns`' complement.** Derived from a forensic pass over all 88 `conflicted` records in `~/.config/mind-meld/pull-history.jsonl{,.1}` (2026-05-01 → 2026-09-01). **47 of 88 — 53% — were pure generator output** with no author on either machine (derived caches + host-reinstalled payload + per-host renders); counting machine-written session state (`pair-review/session.yaml`, `full-review/session.yaml`) takes it to 56 of 88 (64%). They cannot be fixed by a better merger, because both machines are *correct* and simply regenerated at different times. Adding a glob is the only fix that converges.

Every figure in this section was recomputed by matching globs against the records, not estimated. The first draft claimed "63 of 88 — 72%", which corresponds to no defensible cut of the data, and credited `skills/.system/*` with 17 conflicts by double-counting the `skills/roadmap/SKILL.md` conflict that belongs to the generated-renders group. Caught by `/ship`'s own CHANGELOG-accuracy check on the release that introduced them.

Proof the mechanism works, and the precedent for trusting it: `analytics/.last-sync-line` conflicted 4× on 2026-05-01, was added to the gstack `exclude_patterns` in v0.11.13, and has conflicted **zero** times in the four months since.

The four v0.12.51 additions, each with the observed conflict count:

| glob | source | n | why it can never merge |
|---|---|---|---|
| `projects/*/decisions.active.json` | gstack | 18 | Derived snapshot, ONE minified `JSON.stringify` line |
| `skills/.system/*` | codex | 16 | Codex's own bundled payload, version-stamped in `.codex-system-skills.marker` |
| `projects/*/brain-cache/*` | gstack | 7 | gbrain per-machine cache |
| `_GENERATED_HOST_SKILL_GLOBS` | codex | 2 | gstack-extend renders one shared skill per host, byte-differently. Historically also opencode; that source was retired in Track 44A (numbered 37B before the 2026-09-02 split). |

**`decisions.active.json` — do NOT "fix" this with a JSON merge strategy.** It is a JSON array of records each carrying a unique `id`, so a union-by-`id` merge in `merge.py` looks obviously right and is wrong: `computeActive()` in gstack deliberately **omits superseded decisions**, so a union would resurrect every decision any machine had retired. Exclusion is the only correct resolution. It is also lossless — the source of truth `projects/*/decisions.jsonl` stays in scope and merges cleanly (37 clean merges on record over the same window), and `gstack-decision-search` self-heals via `rebuildSnapshot()` when the snapshot is absent or empty. Pinned by `test_gstack_derived_cache_globs_match_observed_paths`, which asserts `decisions.jsonl` and `decisions.archive.jsonl` are NOT matched by either new glob.

**`_GENERATED_HOST_SKILL_GLOBS` is kept as a shared constant even though only the codex entry consumes it after Track 44A retired the opencode source.** The symmetry with opencode is historical — the same logical skill used to be rendered twice, and excluding it from one host only left the other half of the pair conflicting on every pull. Track 47A (the sync-surface card) inherits this mechanism rather than re-extracting the list. The list is still splatted (`*_GENERATED_HOST_SKILL_GLOBS`) rather than shared by reference because `get_default_source` hands these lists to callers that mutate them — pinned by `test_codex_entry_carries_every_generated_host_skill_glob` and `test_generated_host_skill_lists_are_not_aliased` (`codex["exclude_patterns"] is not _GENERATED_HOST_SKILL_GLOBS`).

**Detection is by name AND by marker (v0.14.0, Track 47A).** gstack-extend drops a `.extend-root` file in each dir it renders. `manifest.marker_skip_globs` returns those directories as **path prefixes, not fnmatch globs** — `_under_skip_prefix` matches `rel_path == prefix` or `rel_path.startswith(prefix + "/")`. A `{rel_dir}/*` glob would be wrong twice over: fnmatch `*` crosses `/`, and a directory literally named `*` carrying a marker would exclude every sibling subtree. `walk_generic_source` and `core._build_exclude_map` both thread the prefixes alongside `exclude_patterns` so `_filter_excluded_paths` strips them from prior/peer manifests too. A marker skip must not generate deletion tombstones, exactly as adding a glob must not. The name-based `_GENERATED_HOST_SKILL_GLOBS` stay as belt-and-braces for dirs that lost their marker; a new gstack-extend skill no longer needs a new glob to drop out of sync.

**Browser profiles (Track 94A).** A directory holding a regular, non-symlink `Local State` file (`manifest.BROWSER_PROFILE_MARKER`, written by Chromium and Electron at the root of a user-data directory) is skipped in every generic source, the same way as an `.extend-root` directory: `marker_skip_globs` returns it as a path **prefix**, never a glob, and no folder name is hard-coded (`chrome-profile`, `signed-profile` and `.dev/user-data` all occur). Differences from `.extend-root`:

- No include-root exemption. A `Local State` on an include root returns that include as its prefix; `.extend-root` there still marks nothing. With `include_dirs = ["."]`, prefix `.` means the whole source, including explicit `include_files`, and the walker and nested-repository inventory return before descending.
- Parents of every selected `include_dirs` entry and `include_files` entry are checked, up to and including the source root, for a regular non-symlink `Local State`. The directory that holds it is the prefix, so an include that starts inside the profile (`include_dirs = ["profile/Default"]`, or `include_files` with no `include_dirs`) is left out of the walk and out of the prior manifest, with no tombstone. `.extend-root` is not looked up in parents and keeps its include-root exemption. A symlink directory is not entered, a symlink marker marks nothing, a missing path is not an error, and the walk never goes above the source root. A directory inside a nested checkout is not probed, because that checkout is already frozen. Strict mode refuses on a read error; permissive mode skips it.
- `_collect_marker_files` looks `Local State` up by name before stat-ing any sibling and returns without descending. A live profile churns, and a strict scan refuses on an entry that vanishes mid-scan, so profile internals are never enumerated or stat-ed.
- No tombstones: `core._build_exclude_map` feeds the same prefixes to `_filter_excluded_paths`, so the first push after the marker appears drops the profile from the manifest without a deletion record.
- A peer's `Local State` goes through ordinary source filters; it is not categorically dropped as a peer-planted `.extend-root` is (`_filter_excluded_paths._conflict_shaped` is unchanged). Where those filters admit it, the local marker stops later republication. A peer can therefore plant one and narrow a subtree: lost propagation, nothing deleted. The resulting local prefix also filters later peer tombstones for the marker; to restore propagation, remove an unwanted marker locally and exclude that exact marker path until its publisher stops listing it. A marker at a selected source root can stop the whole source, not only a subtree.

Transition residual: until every Mac runs this release, an un-upgraded peer keeps publishing profile files, and a migrated Mac without that profile can receive them on pull. The new artifact allowlist admits document types but drops the extensionless `Local State`, so those unmarked fragments can be republished indefinitely, even after the fleet upgrades. With no allowlist the marker can arrive too. There is no pull-side profile detection. Narrowing deletes nothing: existing copies require manual identification, exact local exclusions and a push before deletion on every Mac, including those without a copy; wait for dormant Macs too (README "Upgrading"). Leave those explicit exclusions in place to prevent stale copies returning, since deleting a marked directory also removes the marker that was protecting it. Deleting a still-published copy without that sequence can mint tombstones, which an un-upgraded peer applies into the 30-day recoverable store. A non-browser directory holding a file named `Local State` drops out of sync silently, which loses propagation but deletes nothing.

The remaining over-exclusion: `roadmap`, `full-review`, `test-plan`, `pair-review`, and `review-apparatus` are **generic names**, and unlike `skills/gstack-*` they carry no distinguishing prefix. A user who hand-authors `~/.codex/skills/roadmap/SKILL.md` on a machine without gstack-extend (and without an `.extend-root` marker) still gets it **silently dropped from sync** by the name glob. Nothing is deleted (exclusion only narrows the manifest), so this is lost propagation rather than lost data, but the user is not told.

**What is deliberately NOT excluded.** `~/.gstack/projects/*/pair-review/` prose artifacts (`deploy.md`, `report.md`, `parked-bugs.md`) stay in scope: pair-review advertises cross-machine resume as a feature. Since Track 94A the allowlist admits exactly those three pair-review files and nothing else at that level. `projects/*/pair-review/session.yaml` IS excluded (v0.14.0) — it is a live per-machine state machine and definitionally cannot be shared. The measurement behind leaving the prose in: the same paths took **31 conflicts against 178 mtime-skips**, so the existing local-is-newer gate already absorbs 85% of these collisions. The fuller fix is device-scoped artifact paths (`pair-review/<device>/`) in gstack, not a blanket mm exclusion.

### Bulk generator output

Agent-written environments, app bundles, archives, binaries and QA evidence
are not sync data even when they never conflict: they cost bytes in every
2.0 snapshot. Track 72B's 16 globs above address the P1 demand measured
2026-10-11 and re-measured 2026-10-13 on device 889e42c0.

Method: a read-only walk of `projects/`, `analytics/` and `retros/`, applying
the walker's global `EXCLUDED` list and the seven prior gstack globs, pruning
nested git checkouts. Match each glob with fnmatch against the relative path.
Sealed size below is per-file `gzip.compress` at its default level, as
`crypto._encrypt_with_master_key` compresses before encryption; the fixed
AES-GCM overhead is omitted. This is a proxy for 2.0 snapshots, which frame
many files per payload, rather than a measured snapshot size.

Measured 2026-10-13, device 889e42c0; overlaps count in each glob row:

| glob | files | raw MB | sealed MB |
|---|---:|---:|---:|
| `projects/*/venv/*` | 3,650 | 62.1 | 18.3 |
| `projects/*/.venv/*` | 0 | 0.0 | 0.0 |
| `projects/*/*.app/*` | 43 | 59.2 | 42.9 |
| `projects/*/.qa-evidence/*` | 4,458 | 20.1 | 4.1 |
| `*.zip` | 2 | 39.1 | 39.0 |
| `*.tar` | 2 | 0.5 | 0.1 |
| `*.tar.xz` | 1 | 19.2 | 19.2 |
| `*.whl` | 1 | 0.5 | 0.5 |
| `*.so` | 6 | 24.3 | 7.7 |
| `*.dylib` | 3 | 7.0 | 2.7 |
| `*.pak` | 4 | 15.3 | 14.7 |
| `*.tgz`, `*.tar.gz`, `*.tar.bz2`, `*.tar.zst`, `*.dmg` | 0 | 0.0 | 0.0 |
| union | 8,156 | 181.6 | 104.9 |
| gstack set before | 35,689 | 1,286.2 | 345.7 |

The union is 23% of files, 14% of raw bytes and 30% of sealed bytes. Archive
and binary bytes mostly sit inside virtualenvs and app bundles, so summing
the categories overstates the saving. In 1.x this trims manifest entries;
own storage is reclaimed by an attended content-changing push's GC or `mm gc`.

`node_modules` is already in the global `EXCLUDED` list. `*.tar*` is deliberately
absent: fnmatch `*` crosses `/`, so that glob also matches `release.targets.md`
and `notes.tar.md`. Directory globs are anchored under `projects/*/` so a slug
named `venv` or `Foo.app` keeps its project notes. Inside a project, directories
named `venv`, `.venv`, `*.app` or `.qa-evidence` are skipped at any depth.
The extension globs match anywhere in the gstack source, including `analytics/`
and `retros/`. Matching is case-sensitive: `*.ZIP` stays in scope.

`tools/`, `fixture/` and `fixtures/` stay in scope: their 741 files / 3.2 MB
sealed (0.9% of the set) include real copied source and test fixtures.
Decision 6 also keeps `compact-snapshots/` and `ranking-summaries/` review
receipts in scope (2026-10-13). No skill reads or writes those names, but an
active project's notes cite the receipts as durable evidence; the names
describe one run's directories, not a generator class. Retire them by deleting
that run's directory when its work closes.

Exclusion deletes nothing on any Mac. There is no default-glob opt-out:
removing a recommended glob from explicit config brings back the status
warning and attended migration prompt, which re-adds it by default.

## disabled_sources + consumer-boundary filter (load-bearing, v0.10.0)
Per-machine source toggle. `[sync].disabled_sources: list[str]` lists source
names to skip on this device only (config.toml is per-machine, never synced).
`get_sources()` filters by name after resolution and before the path-existence
filter. Retired source names (`RETIRED_SOURCE_NAMES = frozenset({"grok", "opencode"})`)
are dropped silently so old configs and peer manifests continue working without
minting deletion tombstones. The default `agy` source (`type: "generic"`) syncs
`skills/`, `rules/`, `AGENTS.md`, and `GEMINI.md` at `~/.gemini/config`, excluding
`agents/`, `mcp_config.json`, `hooks.json`, `skills.json`, and `projects/`. Like
`muse`, it never auto-activates on legacy configs without explicit `[[sync.sources]]`
opt-in; `mm init` and `mm reconfigure-sources` treat it as detected/available
when either `~/.gemini/config` or `agy_sessions_root()` exists. The default
`muse` source (`type: "muse"`) has its own walker shape (`memory/` hardcoded
at `~/.local/share/muse`, `.muse-memory.lock` excluded on push and pull) but
never auto-activates: Muse creates `memory/` itself on first run, so neither
the data root nor that tree is consent. Only `mm enable-source muse`, or an
explicit "y" at the `mm init` or `mm reconfigure-sources` prompt, adds it.
Both prompts default to N. CLI surface: `mm enable-source <name>` / `mm disable-source <name>` /
`mm reconfigure-sources` (top-level kebab-case to match `mm migrate-config`
pattern). Strict by default; `--force` accepts unknown names for forward-compat
(pre-disable codex before it ships).

`_push_core` uses `_filter_unselected_sources` on the prior manifest BEFORE
`generate_tombstones`, covering ok-fetch / sidecar / peer-fallback uniformly.
`_filter_disabled_sources(manifest, disabled)` filters pull's peer manifests
BEFORE `collect_tombstones`; it always drops retired source names, including
when the disabled list is empty. Status and diff use it with an empty disabled
list to ignore retired sources while retaining ordinary disabled-source changes.
Disable-then-exclude order drops whole sources before walking their patterns.
The filter MUST NOT apply at
`_fetch_remote_manifest` itself — `mm gc` reads its own raw manifest via that path
and a filtered manifest there orphans live own blobs (codex-2 #1 hazard,
mirrored from exclude_patterns).

**Tombstone-suppression invariant.** Disabling a source on machine A and
pushing must NOT generate deletion tombstones for that source's files.
Spurious tombstones suppress restoration and propagation of a missing path
across upgraded and stale peers until expiry (`manifest.TOMBSTONE_TTL_DAYS
= 30`, re-broadcast newest-wins by `collect_tombstones`). Existing local
bytes are never removed. Re-enabling brings the source's files back as
fresh entries (not tombstones). Sidecar recovery filters too so a corrupt-
manifest recovery on a freshly-disabled config doesn't re-introduce
disabled-source paths via the sidecar. All five scenarios (push, re-enable,
pull, sidecar recovery, gc) plus the Track 44A retirement transition pinned
in `tests/test_integration.py::TestDisabledSourcesTombstoneSuppression`.

`seen_sources.py` (new module, mirrors pullhistory.py shape) tracks per-machine
acknowledgment of source names at `~/.config/mind-meld/seen-sources.json`
(0600). `read(initial)` lazy-initializes under `fcntl.flock` on first call,
seeded with the names of currently-resolved sources. **Migration invariant**:
without the lazy-init seed, every existing user's first post-v0.10.0 `mm
status` would surface spurious "New source: claude!" / "New source: gstack!"
hints for sources they're already syncing. Pinned by
`test_seen_sources_initialized_to_existing_on_upgrade`.

`mm status` surfaces two breadcrumbs: "Disabled sources (this device): X, Y"
when the disabled list is non-empty, and "New source available: X" (one-shot
via `seen_sources.compute_new_sources`) when DEFAULT_SOURCES grows on upgrade
and the user hasn't yet enabled or disabled the new name. `mm sources` shows
all configured sources (not just resolved) with an Enabled column; disabled
rows render dimmed.

The `_prompt_source_toggle(source, *, current_state)` helper (extracted from
`_prompt_sources` in v0.10.0) is the single source of truth for the per-source
Y/N prompt copy + default rule. `_prompt_sources` (init) and
`reconfigure_sources` both call it; `mm init`'s default-Y-on-path-exists
behavior is preserved, except that Muse always defaults to N (see above).

**`has_explicit_sources` gates on key presence, not list truthiness (Track 61A
pre-landing review).** `enable_source`'s `currently_active` check and
`reconfigure_sources`'s equivalent both need to tell an explicitly-emptied
`sync.sources: []` apart from an absent key — the two are indistinguishable
under bare `not explicit_sources` (list truthiness), which reads an empty list
as "no explicit sources configured" and falls back to defaulting every prompt
from `default_names`. `enable_source` already computed
`has_explicit_sources = "sources" in sync` for this; `reconfigure_sources`
still used the bare-truthiness form, so a user who explicitly cleared
`sync.sources` had every `DEFAULT_SOURCES` prompt silently default back to Y
on the next `mm reconfigure-sources` run. Fixed by giving `reconfigure_sources`
the same key-presence test. A new `sync.sources` consumer should use it too.

## `walk_generic_source` filesystem-identity dedup (load-bearing, v0.10.1)
Mirror of `_find_conflict_files`'s dedup at the manifest-walk layer. When `include_files` overlaps `include_dirs`, the same on-disk file lands in `collected_paths` twice. Pre-v0.10.1, the second pass got hashed and overwrote the first manifest entry — wasted CPU on identical bytes. On case-insensitive volumes (APFS default) with case-mismatched config, two distinct rel-keys could be created for one inode — a real correctness bug producing phantom add/delete fleet churn.

Dedup uses `set[tuple[int, int]]` keyed on `(st_dev, st_ino)`. Sort `collected_paths` by relative-to-base path BEFORE the dedup pass so the rel-key kept on hardlink/symlink overlap is deterministic across runs and across machines (include_files entries follow include_dirs results in config order, and enumeration order varies across machines). Without the sort, two peers walking the same tree could pick different rel keys for the same inode and generate phantom add/delete churn in the manifest diff. Sites: `manifest.py:walk_generic_source` (the pre-hash loop). A permissive walk still skips a stat failure. A strict publishing walk does not: it re-probes a vanished file once, then holds it when a collector is present or refuses (see Complete snapshots above).

## Pull-time case-collision detection (load-bearing, v0.10.1)
A Linux peer can legitimately have BOTH `Projects/x.md` AND `projects/x.md` (case-sensitive ext4). A macOS APFS puller can only represent one — the second WRITE would silently alias / overwrite the first via inode collision. Pre-v0.10.1, this was a silent data-loss hazard.

`_detect_case_insensitive_fs(path)` is a non-invasive probe (no writes): when the root is missing, find its nearest existing ancestor, then compare that basename's swapcase variant with `samefile()`. An absent alternate spelling proves case sensitivity; an inconclusive probe (I/O failure or a case-neutral basename) conservatively treats the volume as case-insensitive. This keeps incoming paths that differ only in case from overwriting each other while restoring into a missing root.

`_detect_pull_case_collisions(manifest_cache, local_sources_map)` aggregates across ALL peer manifests so a collision between peer A's `"Projects/x.md"` and peer B's `"projects/x.md"` is detected even when neither peer alone exposes both casings. Returns clusters keyed by source name AND casefold key. It probes `base_path.resolve()`, not the source's on-record path: `local_sources_map` deliberately keeps a symlinked mm-events custom root unresolved (so ownership/bootstrap checks stay symlink-aware, see `_normalized_mm_events_path`), but that same unresolved path is the wrong input here — a source root symlink can cross a volume boundary with different case-sensitivity than the link's own parent, and this probe must judge the filesystem the bytes actually land on.

`_drop_case_collisions_from_manifests(manifest_cache, collisions)` returns a NEW cache (input not mutated) with all-but-lex-first paths dropped per cluster. Tombstones are NOT touched — collision is about per-pull WRITES on a case-insensitive consumer; tombstones encode prior consensus and stay intact (mirrors the asymmetric `_filter_disabled_sources` invariant). Manifest keys are NOT case-normalized GLOBALLY — only consumer-side WRITE skipping. Cross-platform peers retain their distinct casing in the synced manifest. The raw manifest stays intact for `mm gc` (which reads via `_fetch_remote_manifest`, unfiltered).

Hook site: `_pull_core` BEFORE `collect_tombstones` and the per-source download loop, AFTER the disabled-sources / exclude-patterns filter chain. Per-cluster `mm: warning:` to stderr names the kept and dropped paths so the user sees what was skipped (visible-failure contract).

## Pull/push history log (Track 73B)

Track IDs 75A to 81A in this section and its subsections name the Tracks from
before the 2026-10-19 spec rewrite, not the current roadmap's Tracks of the
same number: 75A to 78A were killed, and 81A's read-only peer status became
Track 92A while its retirement was killed. The four provisional tables below
have no current reader; whichever current Track first needs per-peer state
(91A's last-synced base or 92A's peer status) decides whether to reuse or drop
them. Obligations this section gives those old Tracks bind whichever current
Track first writes non-history `state.db` data; README's History database
section already names Track 91A for the move-aside remedy.

`pullhistory` owns the forensic `history` table in the local, never-synced
`~/.config/mind-meld/state.db` (0600; a directory statedb creates is 0700, an
existing one keeps its mode). `statedb` is a leaf:
standard library plus `fsutil` only, lazy `sqlite3`, no cached connections.
`connect(read_only=...)` closes on exit. `transaction` uses explicit
BEGIN IMMEDIATE / COMMIT / ROLLBACK and refuses nesting. Every SQL statement
in history runs under `statedb.translate`, including execute/fetch failures.
Error classification checks extended SQLITE_READONLY_ROLLBACK (776) first,
then primary codes (`code & 0xFF`): busy/locked, corrupt/not-a-database, or
unclassified. Missing sqlite3 has the same reinstall remedy as the Cursor reader.

New files use exclusive, no-follow creation at 0600; existing files must be
regular and are tightened by path, never opened outside SQLite (closing an
extra descriptor could drop SQLite's POSIX locks). The pytest account-state
write guard runs before every writable open or filesystem mutation. Busy
wait is 5000 ms (`BUSY_TIMEOUT_MS`; contention tests use 100 ms).
`synchronous=FULL`, without `fullfsync`, applies per writable connection.
Only an empty new database gets persistent `auto_vacuum=INCREMENTAL` and
`journal_mode=DELETE`, outside a transaction. Connect never resets journal
mode: 75A may migrate it to WAL after measuring ledger durability. No foreign
keys exist. 76A decides when to run incremental_vacuum.

Migrations are forward-only tuples of individual SQL statements, all pending
steps and `user_version` changes in one transaction. The script execution API
is forbidden because its implicit commit breaks migration atomicity. Before
migration or persistent settings, `meta.compat_version` refuses a file requiring
more than this build's schema v1. Newer compatible `user_version` values are
used as-is, never migrated backward. Only changes to released readers' tables
(`meta`, `history`) require a compatibility bump. Read-only opens never migrate,
import, create files or change settings. A zero-byte file, version 0, absent DB
or missing import marker uses the legacy JSONL reader. The URI is percent-quoted
and has `mode=ro`. A hot rollback journal raises the typed read-only error;
every non-preview push/pull/auto command and `mm recapture` repairs it under the
mm lock before work, even on an unchanged sync. Recovery failures never fail
sync. `mm log` rechecks compat_version inside its read snapshot, and every
write transaction rechecks it after BEGIN IMMEDIATE; malformed
stored values (compat_version, legacy_jsonl_stat, a row's extra) are corrupt.

A statedb write transaction is always the **innermost** lock: never acquire the
mm lockfile or upgrade-state's flock inside one. statedb itself takes neither.
History can be written outside the mm lock (the self-upgrade hook releases its
upgrade flock first); the four 2.0 tables may be written only under the mm lock
by their delivering Tracks.

`append` and `append_self_upgrade` retain their legacy dict shapes; `sidecar=`
is removed, but imported sidecar keys survive. `row` stamps `ts` at append time.
`write_rows(conn, rows)` only inserts inside its caller's transaction, rejects
unknown new keys and never imports, prunes or commits. Every UTF-8-encodable
string goes into its named history column; other values, unknown legacy keys
and lone surrogates go into sorted, ASCII-escaped JSON `extra`. NULL columns
are omitted when decoding, preserving absent versus explicit-null keys.
`ts_us` is integer UTC microseconds (naive timestamps are UTC), NULL on parse
failure. It and `id` are private and never emitted.

The first flush stats both legacy files, then imports `.1` then live
`pull-history.jsonl`, skipping invalid JSON/UTF-8 (and over-deep) lines and
individual malformed rows (SQLite TOOBIG, CONSTRAINT, MISMATCH, RANGE or a
non-SQLite row error), and commits the import marker with the rows. Any other
SQLite failure, or one that ended the transaction (FULL, IOERR), aborts the
whole flush so a later flush retries the import; it never autocommits a part. The files remain inert. Non-ENOENT OS errors abort the entire
legacy import, but still set `history_imported` and sanitized
`history_import_pending`: new rows stay visible, `mm log` notices, and a later
flush or maintenance retries the whole import exactly once. Deferred rows get
ids below the current minimum. Successful import records each legacy file's
size and mtime_ns (or absence) taken before reading, without inode/device, so
an older mm's append during the import also fires the notice. `mm log` stats both files;
a changed or newly present file yields the stale-binary notice, evidence of
past divergence rather than proof an older mm still runs. Moving both aside
silences it. Neither import nor log reads `~/mm-history-backup/` copies.

`batch()` buffers in a ContextVar, binds its database path at entry, joins an
outer batch, flushes at 1000 rows and in finally, including SystemExit and
KeyboardInterrupt (which still propagate). Empty batches never connect.
`_pull_core` covers bookkeeping and exclusions; each `_push_core` call (push,
autopush, recapture) is one batch, so per-source upload batches join it.
A failed flush drops its buffer without retry. `command_scope`, entered before
config/transition handling, skips every later history write and recovery after
the first failure: at most one busy timeout per command. Attended push/pull/
recapture print one plain `mm: notice: history not recorded ...` line; auto commands stay
silent. History swallows Exception only; the pytest write guard and other
BaseExceptions propagate. Nothing new is persisted for failure reporting.

Retention keeps the newest 20,000 ids **per known (verb, action) class**, NULL
matched with IS; unknown future classes are never pruned by an older binary.
Per-file push `uploaded` rows stay; no summary action exists. This replaces the
shared 1 MB ring: excluded rows cannot evict conflicted rows. About 8,000
excluded rows per attended pull means at least two complete exclusion audits;
conflicted rows generally cover years. Measured ~263–400 bytes per row and
roughly ten full classes suggest ~80 MB, a workload estimate, not a bound.
Re-measure for 75A/77A receipts. History is forensic: receipts read by sync logic
belong in their owner's table or a separately retained class. A SIGKILL or
power loss can drop up to 999 buffered rows; a failed flush drops its buffer.
History makes no hard power-loss durability claim on macOS: rollback-journal
corruption is possible. Measured 4,500 commits cost 1,640 ms versus 5.1 ms
batched; eight 1000-row fullfsync commits cost 124 ms. 75A re-decides durability
before ledger use.

`read_records()` materializes insertion order; an explicit path stays the
tolerant JSONL reader. `query` filters in SQL (ts_us for --since), orders
`ts DESC, id ASC` and applies positive limits (clamped to SQLite's integer
range). A filter value that is not UTF-8-encodable matches no table row (such
values live in `extra`). The legacy path retains the
same filtering and stable sort. All matching compact tuples are loaded in one
read transaction, then the connection closes before decoding/printing.
`--limit 0` memory grows with retained rows; no paging or partial stdout on
read errors. Table titles name state.db or legacy pull-history.jsonl. Cells use
safe_str; jsonl keeps raw sorted-key legacy objects. Errors are one plain,
sanitized stderr line, exit 1, empty stdout. `pullhistory.explain` owns pinned
error/notice strings and errors.STATEDB_HELP_URL.

**History-only remedy boundary:** the corrupt-database move-aside advice in
README is valid only while the four provisional tables remain unused. 75A
must replace it before writing the first ledger row; 76A owns authoritative
recovery with pending publications and unexpired displaced content preserved.

### Schema columns and readers

All six tables and four indexes are migration 1's pinned DDL in statedb.py;
tests assert table_info and index_list independently.

| Table | Columns and readers / delivering Track |
|---|---|
| meta | key/value: compat_version gates every connection; history_imported selects history; history_import_pending reports deferred import; legacy_jsonl_stat feeds the two-file stale-binary notice (73B). No other keys yet. |
| history | id orders insertions/prune/ties, ts renders and sorts, ts_us filters --since, verb/device/source/rel_path/action/local_sha/remote_sha/extra reconstruct mm log rows (73B); old_version/new_version live in extra. |
| ledger | source/path key; sha/vv/mtime/deleted/pending: 75A Publish: ledger authoring, commits and presence, and 78A Causal classification, symmetric resolution and displaced; size: no reader. vv is sorted compact canonical JSON; no content bytes. |
| cursors | peer/applied_seq/hash/gap_seq/gap_since: 77A Apply cursors, gap rule and snapshot bootstrap; hash anchors chains, NULL after snapshot raise. 81A Delivery receipts, device projection and retirement reads status; 75A presence reads applied. |
| own_log | seq/prev/hash/parts/bytes: 75A publish; hash chains prev. time: integer epoch seconds for 76A Snapshots, compaction and own-storage reconciliation (14-day compaction). No redundant created_at. |
| displaced | id/source/path/sha/vv/size/mtime/deleted/content/reason/time/expired: 78A mm displaced and classification; 76A compaction expiry. size remains visible after expiry. |

ledger, cursors, own_log and displaced are **provisional and empty**, with no
current reader (see the note at the top of this section). A Track that adopts
one may DROP and re-CREATE it in a migration without raising compat_version:
released binaries read only meta and history.
`expire_displaced` is unit-tested only until 76A calls it inside its
transaction: at 30 days it clears content and sets expired, never deletes rows
or changes metadata.

### History API recipes

These fenced Python blocks are extracted and executed by tests against an
isolated DB_DIR. Read through the public query API:

```python
from mind_meld import pullhistory, statedb
records = pullhistory.query(limit=5)
```

A batch flush imports, inserts and prunes in one transaction:

```python
rows = [pullhistory.row(verb="push", device="example", source="gstack",
                       rel_path="note.md", action="uploaded")]
with statedb.connect(read_only=False) as conn, statedb.transaction(conn):
    pullhistory.import_if_needed(conn)
    pullhistory.write_rows(conn, rows)
    pullhistory.prune(conn)
```

75A inserts receipts inside its publish transaction, closes the connection,
then maintains separately. Best-effort maintenance cannot roll back publish:

```python
with statedb.connect(read_only=False) as conn, statedb.transaction(conn):
    # 75A's ledger and own_log updates belong in this transaction too.
    pullhistory.write_rows(conn, rows)
pullhistory.maintain_best_effort()
```

## Corrupt-manifest recovery (load-bearing)
`_fetch_remote_manifest` returns a tri-state `ManifestFetch(status: "ok"|"missing"|"corrupt", manifest)`. On `corrupt` (or `missing` with `vanished`, see [Syncthing-tolerant reads](#syncthing-tolerant-reads-and-waiting-track-87a)), `push` runs a recovery chain before writing a new manifest: (1) local sidecar at `~/.config/mind-meld/last-push.json` (preserves this device's fresh deletions), (2) peer-manifest tombstone aggregation (propagated deletions only), (3) refuse with actionable error. Never treat corrupt as empty — that silently un-deletes files fleet-wide. An `ok` result built only from conflict copies (canonical absent or unreadable) sets `conflict_copies_only`; push then bypasses its no-op gate and rewrites the canonical from local truth, diffing against the merged copies so unchanged blobs are not re-uploaded. Older copies can predate this device's latest deletions, so `_recover_prior_manifest` merges the local sidecar into the prior (tombstones, source retention, nested-checkout freeze) with a warning, even in autopush, that also says recent deletions may be lost when no usable sidecar exists. Like other recovered priors, it freezes nested-checkout entries only when their blob is still stored. `mm gc` refuses when its own manifest is corrupt or conflict-copies-only (referenced own blobs may still be live); peer manifests are outside its collection scope. See SPEC.md "Manifest corruption recovery" and "Merge invariants" for the full invariant.

## Syncthing-tolerant reads and waiting (Track 87A)

Syncthing delivers files late and out of order, replaces them with a rename and
leaves temporary and conflict files beside them. Storage reads tolerate that
without treating it as corruption or failure. Tests use one shared folder,
`tests/syncthing_folder.py` (late delivery, manifest before blobs, removal after
listing and during a read); no sleeps: the retry clock is injected.

**One name classifier** (`storage/local.py`: `is_foreign_name`, `is_foreign_key`,
`is_conflict_name`, `is_conflict_of`) serves `list_keys`, `find_conflict_copies`,
the device table and `mm diag`. *Foreign*: any dot-prefixed component
(`.syncthing.<name>.tmp`, `.stfolder`, `.stversions/`, `.DS_Store`) or a
`~syncthing~` name. `list_keys` never returns a foreign key and does not descend
into a foreign directory; `list_foreign_keys` returns them for diag. *Conflict
copies* are iCloud (`name 2.ext`), Dropbox (`(conflicted copy ...)`) and
Syncthing (`<stem>.sync-conflict-YYYYMMDD-HHMMSS[-<7 chars>]<ext>`, stacked, or
extensionless like `mm-crypto-init.sync-conflict-...`). `mm-crypto-init.preserved-*`
(written by releases before Track 89A, never by this one) never matches. A candidate that disappears before validation is skipped silently.

**`LocalBackend.get(key, *, expected=False)`** opens the file directly (no
`exists` pre-check). Only `FileNotFoundError` is absence and raises
`StorageNotFound(key, vanished=)`; every other `OSError` (EACCES, ENOTDIR) stays a
`StorageError` failure. `expected=True` means the caller listed, `exists`-checked
or conflict-scanned the key, so ENOENT from the first open is a replacement in
flight: retry on `_VANISH_RETRY_DELAYS_S` (0.05, 0.1, 0.2 s, about 0.35 s per
file) within a per-backend-instance budget of about 2 s (one backend per
command), then raise with `vanished=True`. A key never seen (blobs, a manifest
that was never there) reads missing at once, with no delay. `sleep` is injectable.

**`ManifestFetch.vanished`**: an observed canonical manifest that is gone after
the retry reads `status="missing", vanished=True` (or `ok` from conflict copies).
A validated conflict copy that disappears at its final read also sets `vanished`
when no manifest remains; that direct read has no bounded retry. Readers treat
these absences as missing, never corrupt. Writers must not read it as a first
push: `_recover_prior_manifest` runs a vanished own manifest through the
corrupt chain (sidecar, then peer tombstones, then refuse with a wait remedy) and
`push` rewrites it (`recovering_from_corrupt`). GC's missing-own-manifest skip
already holds. Pull must not read it as waiting either: the own manifest carries
this Mac's deletion records, which are not applied that pull, so a file deleted
here can come back from a peer. `_pull_core` adds it to `corrupt_peers`
(`_CorruptPeer.own_vanished`), which keeps the load-bearing stderr warning and
`corrupt_peer_count`, so `mm autopull` records `degraded` (detail
`N corrupt peer manifest(s)`). `mm status` must not read it as a first push:
`_status_verdict(manifest_arriving=)` opens with the still-arriving verdict
(see [Status verdict](#status-verdict-and-attended-sync-evidence-track-77a)).

**One device inventory per pull** (`devices.inventory_devices`, `DeviceInventory`:
`valid`, `dropped`, `vanished`, `foreign`) feeds the fleet gate, peer selection,
tombstone collection, early returns and the waiting notice, so a replacement
between two reads cannot make them disagree. A file counts as a device only when
it is `devices/<id>.json` holding `device_id == <id>`. Conflict copies, misnamed
files and a body whose id is not its file name are foreign: never a device, never
a refusal. A key-named unreadable or shape-invalid entry is `dropped` and still
refuses the pull (Track 5E). A key-named entry that vanished mid-read stays in the
manifest set (its tombstones still apply) and is skipped only for the version
check and for pulling its files; it is reported as waiting.

**`waiting` outcome.** Per file: `_download_and_apply` maps `StorageNotFound` on a
blob to `waiting` (an `ApplyOutcome` key). Nothing was applied, nothing failed, no
`pullhistory` row is written, and it is never `degraded`. Available files from
the same peer still apply. A peer is fully applied only when it has zero waiting
and zero failed files; manifest conflict-copy cleanup waits for that point
(a kept copy may be the only holder of the manifest the missing blob belongs
to), and a waiting-only device gets no
`device_names` entry. `PullResult.waiting` holds per-peer `WaitingPeer` facts
(files, vanished manifest, vanished device entry) and `total_waiting` counts
files. `waiting_clause` builds `waiting for 2 files from Mac B, Mac C's manifest,
cccccccc's device entry`, appended to the attended success line and placed before
`Run:` on a failed one; `waiting_detail` is the autopull `success` breadcrumb
detail (`mm status` prints it). Attended runs record no waiting count. A published
peer's not-yet-arrived manifest and an attended waiting count in `mm status` stay
with Track 92A. `mm diag` adds `ignored_storage_files`; its manifest and device
totals count valid keys only.

## Manifest read-path invariant (load-bearing)
Every manifest loaded from bytes/disk MUST go through `manifest.load_manifest(bytes) -> dict`, which composes `deserialize_manifest + normalize_manifest` plus full inner-shape validation. The function guarantees the returned dict has dict-typed `sources` and `tombstones`, each source has a dict `files`, each file value is a dict with a string `sha256`, and each tombstone value is a dict. Non-string file mtimes normalize to `None` only after the file shape is validated. Malformed manifests raise `ManifestError` at the load boundary instead of crashing downstream consumers (`_merge_manifests`, `collect_tombstones`, `generate_tombstones`, the diff loop) with `AttributeError`. `_fetch_remote_manifest` already catches `ManifestError` and falls through to the recovery chain, so a malformed peer manifest degrades to a clean "corrupt" status. Do NOT add a new manifest-load path that bypasses `load_manifest` (sidecar.read uses `deserialize_manifest + structural-check + normalize_manifest` deliberately, to preserve the anti-tampering guard on raw input).

## Pull-time mtime preservation (load-bearing, v0.12.3)
`_apply_write` and `_apply_conflict` (pullflow.py) MUST call `_restore_mtime_best_effort(path, remote_info.get("mtime"))` after a successful `atomic_write_bytes`. Without it, every pulled file lands with `st_mtime = now-of-pull`, and any downstream consumer that orders by mtime (e.g. gstack skill preambles' `ls -t` recency scan over `~/.gstack/projects/*/checkpoints/`) sees freshly-pulled-old files as "newer" than locally-authored newer ones. The keep-remote interactive branch in `_apply_incoming_file` does the same restore inline.

`_apply_merge` and the merge-via-LCS interactive branch deliberately do NOT restore — line-union merges produce locally-authored content, and backdating to the remote mtime would cause peers' next pull to see `local_mtime <= their remote_mtime` and skip the merged result, losing the union content fleet-wide.

**Future-clamp invariant.** `conflictmtime.py:_restore_mtime_best_effort` caps the applied mtime at `now + _MTIME_RESTORE_MAX_SKEW_SECONDS` (60s). Without the clamp, a peer with a bad clock OR a passphrase-holding attacker minting a manifest dated in 2099 would poison the victim's local mtime into a permanent `local_mtime > remote_mtime` skip at the `pullflow.py:_apply_incoming_file` mtime gate — silently locking the victim out of all future legitimate updates to that path. The 60s window absorbs normal NTP drift between Macs without admitting year-2099 abuse.

**Defensive parsing.** `_restore_mtime_best_effort`, the apply mtime gate, and `pullplan._predict_pull_outcome` catch `TypeError | ValueError | OverflowError | OSError`, including comparison of a timezone-less remote timestamp with the timezone-aware local mtime. `mtime_from_manifest` rejects timezone-less strings. `load_manifest` rejects non-dict file values and missing/non-string `sha256` with `ManifestError` before coercing any non-string `mtime` to `None`. A malformed entry degrades to the existing corrupt-peer warning; other peers continue. An existing divergent file with `mtime=None` takes the conflict path. Consumer guards also protect direct callers and local-clock overflow. `_upload_changed_blobs` takes freshly scanned local revisions, never loaded peer metadata.

## Apply exception boundary (Track 53A)

`_download_and_apply` contains exactly `(OSError, MindMeldError)` around `_apply_incoming_file`. An unrecorded apply becomes `failed` and later files, sources, and peers continue. `typer.Abort`, `SystemExit`, `KeyboardInterrupt`, programming errors, and every other exception propagate. Never widen this boundary to `except Exception` (Abort is a RuntimeError subclass).

A per-source `_ApplyReporter` owns outcomes, byte accounting, deferred-bump invalidation, and touched parents. Each file starts with `begin`; all five publication sites call `published` immediately after successful `atomic_write_bytes`, before mtime restore, cleanup, or output. Publication and normal return must agree; different words raise `RuntimeError`. Unchanged/skipped/failed and deduplicated conflict returns are recorded at the boundary if nothing was published. No disk reconcile: matching bytes cannot prove this attempt wrote them. Direct helper calls default to diagnostics without a ledger.

Every apply failure uses `_warn_apply_failure`, on stderr in quiet and interactive modes. Interactive progress is constructed with `redirect_stderr=False` so those lines stay on the real stderr while the widget runs. Every dynamic field uses `safety.safe_terminal_str` to enforce one physical line, including device/source/path, operation, preservation text, and exception. Errno comes from the exception or its cause (atomic writes wrap OSError in StorageError). The actual first existing non-directory ancestor is named as a folder/file conflict with the peer. `_print_apply_warning` swallows only output `OSError`. A contained exception after publication preserves the outcome and prints a notice naming the publication and failed follow-up; stale-sidecar cleanup warnings and unreadable-sidecar scan warnings use the same non-raising printer. Directory creation is one component at a time so a later mkdir failure still registers every directory that landed. iCloud/Dropbox/Syncthing manifest conflict-copy cleanup waits until that device had no contained apply failures or waiting files.

`_pull_core` sets `in_flight` before calling the source and clears it after bookkeeping. Aggregate accounting is separate from `_record_source_bookkeeping`; history and sync-log attempts are tracked so recovery never replays already attempted writes. On `BaseException`, partial outcomes are counted once, `_fsync_touched_parents` runs first (its warnings assigned to the enclosing summary), bookkeeping runs under an `(Exception, SystemExit)` guard, and the original exception is re-raised. A summary or progress teardown failure cannot replace an in-flight exception; a summary failure on normal completion still propagates. Deferred inline bumps drain only in the normal try path.

Directory durability includes each published file's parent and the parent of every newly created directory, including the topmost ancestor. The set is shared with recovery so a later failure cannot discard it. `_fsync_touched_parents` collects both `StorageError` and raw `OSError` (including directory-close failures) as warnings so recovery continues through the remaining parents and bookkeeping. Ctrl-C waits for this bounded fsync work.

**Residual windows.** An interrupt between `atomic_write_bytes` returning and `published()` loses that file's row and its parent fsync registration; its bytes are already on disk. Closing that window requires a callback inside the atomic-write primitive and is outside this contract. A second Ctrl-C during recovery can interrupt bookkeeping and replace the first traceback. No rollback is claimed.

Attended pull uses `Pull incomplete [result-degraded]:` for a positive failed-file count. Interrupt and unexpected-error lines say completed changes remain. The legacy `Pull incomplete:` summary, and interrupted/aborted lines that say completed changes were kept, remain only for a direct caller with no outcome owner. Success output never includes "failed". Autopull retains the byte-identical breadcrumb detail `N file(s) failed` and exit 0; its count line explains retry on the next pull. Attended pull exits 1 for per-file failures after finalization (78A). `mm status` points a degraded failed-file breadcrumb back to `mm pull`.

Tests: `tests/test_pull_helpers.py::TestApplyExceptionBoundary53A`, publication/failure matrix tests in that module, `tests/test_integration.py::TestApplyRecovery53A`, and `tests/test_manifest.py::TestLoadManifestApplyMetadata53A`.


## rel_path traversal defense (load-bearing, v0.11.21, security)
`manifest._validate_rel_path` rejects any `sources[*].files` key (and any tombstone path part) that could escape its source root when concatenated to `base_path`. Rejection criteria: empty string, null bytes, leading `/` or `\` (absolute path — Python's `Path('/base') / '/abs'` returns `Path('/abs')` so the right-hand side wins), Windows drive letters (`C:foo`), or any segment equal to `..` after splitting on either separator. **Do NOT pre-`posixpath.normpath`**: `normpath` collapses `a/..` to `.` and would silently drop the suspicious segment before the check fires.

Threat model: a peer with the storage passphrase can mint an authenticated manifest with arbitrary UTF-8 keys — the GCM authenticator only proves "the bytes came from someone with the passphrase," not "the bytes describe a confined path." Without this guard, `_download_and_apply` (pullflow.py) would build `local_path = base_path / rel_path` and `mkdir(parents=True, exist_ok=True) + atomic_write_bytes(local_path, ...)` decrypted blob bytes anywhere the user can write — `~/.ssh/authorized_keys`, `~/.zshrc`, `/etc/cron.d/*`, etc. — escalating passphrase + storage-write into RCE on every fleet device that pulls.

Defense-in-depth: `_download_and_apply` ALSO checks `local_path.resolve(strict=False).is_relative_to(base_path.resolve(strict=False))` before calling `_apply_incoming_file`. The dual check exists because `manifest.load_manifest` is the canonical load boundary, but legacy on-disk caches and test fixtures could in principle reach the apply path through a different route; the resolve+is_relative_to assertion catches anything that slips past the load-boundary validator.

## Symlink policy at the manifest and apply boundaries (load-bearing, v0.12.17)

Symlinks **below** a source root are local routing, not synced content. Strict publishing scans omit descendant symlinks for Claude, generic, Grok, and Muse; a symlinked source root remains allowed. Prior-state filtering suppresses those omissions for all four types before deletion proof so a converted link is not tombstoned. The source root itself may be a symlink: users can locate an entire source through a link, and resolving that root remains valid. Do not weaken the rel-path traversal defense to admit child links. An unavailable symlink/privacy probe refuses rather than becoming an intentional omission.

The Grok and Muse walkers are stricter still: each rejects a candidate whose
inode has more than one link. A hard link to `auth.json` or a session transcript
inside an allowlisted `skills/` or Muse `memory/` path has no symlink component, but would otherwise evade
the scoped-source privacy boundary. Do not replace this conservative refusal
with an inode-path search that can race the filesystem walk.

The omission is tombstone-safe only because `_push_core` filters the recovered prior manifest through `_filter_symlinked_paths` before `generate_tombstones`. That filter removes matching file entries and tombstones using the current local source paths. It applies equally to normal fetches, sidecar recovery, and peer-fallback recovery, and protects existing explicit configurations that have not yet migrated to the default `exclude_patterns`. Never move it into `_fetch_remote_manifest`: `mm gc` must keep seeing raw manifests when retaining referenced blobs.

On pull, `_download_and_apply` checks each destination before containment resolution. It returns `skipped` with a breadcrumb when the destination itself is a symlink (including dangling) or any component strictly below the source root is one. This prevents `atomic_write_bytes` from replacing a local link or following it outside the source. `_apply_incoming_file` repeats the leaf check for direct callers. A symlinked source root remains allowed. Skipping is intentional rather than a conflict: a conflict outcome would create a new `.sync-conflict-*` sidecar on every pull beside a link that can never become writable.

Default Codex exclusions additionally cover the known generated skill namespaces (`skills/gstack-*`, `skills/log-work/*`, and `skills/retro-fleet/*`) while leaving hand-authored skill trees syncable. v0.12.51 extended that set: `skills/.system/*` (Codex only — its own bundled payload) and `config._GENERATED_HOST_SKILL_GLOBS` (gstack-extend's per-host renders; historically shared with the opencode source, which Track 44A retired). See "Generated files are not sync data" above for the counts and the reasoning. `manifest.GROK_EXCLUDE_PATTERNS` deliberately did NOT follow; the omission is a measured absence documented at that constant. These globs are migration-safe through the existing exclude consumer-boundary filter; dynamic symlink filtering remains the forward-compatible safety net for new installer layouts and explicit source configurations.

Honest writers (`manifest.walk_*`) build rel keys via `path.relative_to(base)` which by construction NEVER produces `..` segments or absolute paths, so the validator only fires on attacker-crafted manifests; legitimate sync is unaffected. Mirrors `storage/keys.py:_validate_component`'s sibling defense for the sha256 component (sha is hex-bounded, rel_path is free-form, so rel_path is strictly the more reachable surface). Pinned by `tests/test_manifest.py::TestLoadManifestRelPathTraversal` (load-boundary rejection: 11 cases) and `tests/test_pull_helpers.py::TestDownloadAndApplyPathTraversalGuard` (apply-site belt-and-braces: 3 cases). Fuzz strategies in `tests/test_manifest_fuzz.py` were narrowed to the new `valid_rel_path_strategy` for round-trip tests; the wild-input invariant (normalize tolerates garbage) remains covered by `arbitrary_dict_strategy` because `normalize_manifest` itself is unchanged.

## Push dry-run setup contract (Track 56A)

`mm push --dry-run` changes nothing except the local lock file, including the lock parent's creation when needed. `_get_config(read_only=True)` skips the entire transition hook, and push skips its second config load because migration cannot run. Migration emits the existing warning; only when both streams are TTYs does it explain that the preview uses current config and the command without `--dry-run` offers `mm migrate-config` (default yes). No confirm prompt runs. Crypto repair and mm-events bootstrap are planned; fingerprint backfill stays in memory. The update-or-nudge tail (`upgrade.update_or_nudge`, v1.3.0) is gated on `not dry_run` in push's finally block, so a preview never checks, installs or nudges. Previews never scan, warm or capture host usage. `lockfile.py` and `_prove_omitted_paths_absent` retain their existing contracts.

**Missing mm-events root (PC3 reversed by Track 59A).** Missing default event files are deletions, even when the root itself or its `events/` child is gone. Preview completes and reports the deletion a real push would publish, except for the activity row already excluded from preview. A missing default root is omitted from the source list passed to the unchanged deletion proof; it remains an empty source in the local manifest. There is no loss check or automatic restore. Missing custom roots follow the warning-and-skip rule above, in preview and real push. Strict read/access failures still refuse.

Successful previews always print pending setup and “Dry run complete. Nothing was changed except the local lock file.” plus the not-previewed scope (host-usage capture, activity row, GC, upload re-reads). Refusals never print completion and carry the lock-qualified suffix. Exit codes remain 0 completed, 1 stopped, 2 usage error. Tests in `TestPushPreviewNoMutation56A` record filesystem/network/keyring mutation attempts and compare the entire isolated tree with pinned mtimes. Track 62A extends this to the commands and explicit inspection exemptions below.

## Preview and inspection contract (Track 62A)

The [README Previews table](../../README.md#previews) is the operator reference.
`push`, `pull`, `gc`, and `recapture` with `--dry-run` may create/flock only the
local mm lock and its parent. `migrate-config --dry-run` and `diff` do not even
take that lock. No config, keyring, history, upgrade cache, crypto repair,
source bootstrap or network mutation attempt is allowed. Read-only Git walks
remain external to the in-process guarantee. All pending version transitions
survive previews/inspections and are recorded by the next mutating caller.

`_get_config(*, read_only: bool)` and `_maybe_prompt_migration(config, *,
read_only: bool)` require an explicit policy. Interactive previews skip the
post-migration reload and never prompt, even on a TTY; their notice names the
current command without assuming push. Pull also skips its entire conflict
discovery/migration sweep, excluded-path history and the update-or-nudge tail
(no check, install or nudge). Real pull
keeps those writes. Success trailers are owned by command wrappers; interrupted
or refused previews never say they completed. Core setup/crypto/fleet/device,
GC and recapture refusals carry the lock-qualified dry-run suffix; config,
passphrase and held-lock errors retain their existing wording.

The test-only `COMMAND_INTENTS62` table classifies every registered command,
recursing through groups, and requires a lock allowance for any callback with
`dry_run` or a `--dry-run` option. Every preview and inspection runs under the
audit window plus whole-tree comparison. Worker threads started during the
window participate; older workers do not. Bytecode writes are disabled,
subprocess launches recorded, and external launches stubbed at their boundary.
Descriptor exemptions match inodes, never descriptor numbers.

Inspection exemptions are exact owned files: status may seed/recover missing,
empty, malformed or corrupt `seen-sources.json` (and create its parent), while
steady state opens O_RDONLY under LOCK_SH. Recovery reopens O_RDWR|O_CREAT
without truncation, acquires LOCK_EX and re-reads before writing, preserving an
interleaved `acknowledge()`. Author-filtered retro-fleet may write
`identity-cache.json` on fresh, stale and missing-cache runs; no-author-filter
has no exemption. Diag's Grok and identity's Git/GitHub subprocess state is
outside mm's in-process guarantee; the devices child has its own audited entry.

## Pull predictions (Track 62A)

`pullplan.py` imports nothing from cli. `_plan_pull` uses it only for dry-run
or fail-mode preflight; predictions never select real downloads. Diff's
single-file annotation uses the same predictor. Each local file is hashed at
most once per source/path. `_PerSourceResult.predictions` supplies per-source
lines, header suppression, totals and parity evidence; fail-mode uses the same
planned outcomes and prints corrupt-peer and unknown-source warnings before
refusing.

After exclusions/tombstones, decision order is descendant symlink → structural
file/directory collision → absent → unreadable → known digest equality →
mergeable → newer local mtime → conflict. Virtual state tracks files and
directories along ancestors. Write sets the remote digest and future-clamped
mtime; merge sets an UNKNOWN digest; skip/conflict/unchanged leave canonical
state untouched. UNKNOWN never compares equal, so merges are an upper bound.
Fail mode refuses conflicts and possible local failures with exit 3, while
mergeable changes from multiple peers no longer cause false conflicts.

Preview omits blob download/decrypt errors, history, sync logs, manifest
conflict-copy cleanup and old conflict renames. The missing default mm-events
folder note reports its creation without creating it. Pull prints an
incomplete-preview line only for corrupt manifests; unknown sources remain
warnings. No output or exit-code change applies to autopull. Twin-tree parity
tests cover encrypted two-peer input, exclusions/tombstones, symlinks,
unreadability, file/directory collisions in both orders and conflict retries.


## Status verdict and attended sync evidence (Track 77A)

After existing authentication, status emits one sanitized, unwrapped stdout
line before notices, title or detail. `_status_verdict` is pure over explicit
observations: blocking setup/storage/format/incomplete scan, conflicts, unresolved
same-verb failure/actionable degradation or uncertain history, current capture
prerequisite/reader blockers, own manifest still arriving, first push, pending content
or forward-only metadata, then “In sync with this device's last push.” Missing own
metadata selects the first-push verdict unless a higher-priority action applies;
missing with `ManifestFetch.vanished` (observed, then gone mid-read; Track 87A)
selects “This device's manifest is still arriving from sync. Run: mm status”
instead. No incoming peer convergence
is measured. Every --source verdict names its source, and all counts use one
materialized diff list. Selection, availability and previous publication must
agree: missing previously published enabled roots, unavailable requested roots,
unknown/disabled filters, zero usable sources and failed conflict inspection
never fall through to healthy. Absent never-used optional sources and deliberate
disables remain normal. Strict source enumeration/hashing detects incomplete
observations; no second host parsing or peer-health scan is added. Frozen
nested entries retain the existing semantics. Scan failure remains visible and
comparison unavailable, never fabricated deletions. Fatal collection restores
streams, reports failure first, retains notices and ends with the fixing command;
authentication stays outside capture. No update, capture, bootstrap or receipt
rewrite is added to inspection; seen-sources remains the sole status exemption.

Attended callback finalization surrounds setup and core. Typed result/error and
local stage facts classify every callback outcome, including no-op, scoped pull,
lock skip and interruption. Only an acquired mm lock is released. Track 78A places the terminal outcome after
capture-finally, maintenance/update, history and run-evidence finalization.
Known accepted content cannot be revoked by later maintenance failure; recovered pull counts are handed off before rethrow; without that receipt,
effects remain unknown. Previews
bypass evidence, preserving their existing local mm-lock/parent exemption.
Pre-callback parsing and SIGKILL are unrecordable.

`attemptlog` owns private local `last-attended-runs.json` beside the distinct
capture receipt, resolved through SIDECAR_DIR at call time. Status and diag are
its readers. Schema version 1 contains at most push and pull, each with aware
UTC completion time, closed outcome/cause, finite nonboolean integer counts
(0..2**53), known content acceptance or null, bounded source/from-device scope,
safe bounded details, omissions, ordering uncertainty and errno name. Generated
JSON <=16 KiB, <=8 details of <=384 chars. No raw exceptions, credentials,
absolute user paths or host logs are retained. Missing custom mm-events paths
normalize to their source label. Unknown/unsafe detail is omitted truthfully.
See README's run-evidence table and events-retro.md for causes and counter units.

Use `locked_json_rmw(retry_intervals=(), on_contention="raise", mode=0o600)`;
check is_locked and write_error. Capture completion once before locking. A delayed
older completion never replaces a newer same-verb completion; conflicting
equal or older timestamp writes retain the newer facts and become ordering
unknown. Delayed writes and clock rollback cannot be distinguished here. Successful independent verb updates preserve both.
Write failure is best-effort, never sync failure; its bounded stderr notice
names verb/known side effects/code, forbids repeating sync solely for history,
and directs mm diag after local access/storage repair. Guard BaseException and
interruptions propagate. No atomic durability/old-byte preservation is claimed
for this truncate/write protocol.

New evidence and autorun inspection use `locked_json_snapshot(blocking=False)`.
Missing is normal absent history, not success; corrupt, empty, malformed,
unreadable, oversized or contended is unknown, with no repair. Reject ordinary
files >16 KiB before parsing; the stat race retains the shared 64 MiB ceiling.
Deep JSON is unknown. Contention permits one read-only status retry then diag;
other damage/ordering/scope uncertainty directs diag without deleting evidence,
blind sync retries or live locks. A later real writer may repair recoverable
records, not necessarily every damaged file.

Project the newest available execution per verb without deleting the underlying
latest auto or attended records. Each kind retains only its latest record per
verb; later writes overwrite it, so overwritten failures are not retained.
Strict valid timestamp ordering and sufficient scope let completed success of
either kind resolve an available earlier same-verb failure. Ties, invalid/naive/future
timestamps cannot clear it. A scoped pull explicitly limits coverage and cannot
prove global recovery. A skip proves neither failure nor recovery; a routine
lock skip may coexist with a healthy complete local comparison only without
known unresolved same-verb failure. Finding zero repositories is advisory,
not lost work. Keep auto hook-age meaning in diag; attended age is not hook
liveness. `last_autorun`, `last_attended_runs`, `sync_run_projection` remain in
both diag formats. Status shows only selected summaries plus current capture
facts; only existing latest_attempt_superseded proof hides a capture row.
No history engine, hold escalation, future capture counters or Track 92A
peer-state assessment is introduced. Track 78A changes exits/tails only as
described above.
