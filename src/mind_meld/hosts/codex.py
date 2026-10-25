"""Local Codex rollout usage reader and diagnostic adapter (Track 84A).

Reads ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` and the flat
``~/.codex/archived_sessions/rollout-*.jsonl`` archive using the shared
host-reader kit in ``mind_meld.host_usage``. Calls ``host_usage`` helpers
module-qualified so test monkeypatches on the shared kit reach this adapter.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from mind_meld import host_usage
from mind_meld.lockedjson import LockContended, locked_json_rmw, locked_json_snapshot
from mind_meld.token_usage import TOKEN_FIELDS, iter_bounded_lines

CACHE_PATH = Path.home() / ".config" / "mind-meld" / "host-tokens.json"
"""Forensic host-reader cache. Deliberately separate from Claude's cache."""

CODEX_SESSIONS_PATH = Path.home() / ".codex" / "sessions"
CODEX_ARCHIVED_SESSIONS_PATH = Path.home() / ".codex" / "archived_sessions"
"""Flat archive (Codex 0.160.0). ``thread/archive`` moves a rollout here from the
date tree; Paseo archives on merge, and the Codex app and VS Code can too."""

_ROLLOUT_NAME = re.compile(r"^rollout-.*\.jsonl$")


class _CacheEntry(TypedDict, total=False):
    """One rollout's cached parse. Two shapes share this map.

    A LEDGER entry carries ``states`` plus the resume carry-set described
    below. A NO-LEDGER entry sets ``no_ledger`` instead and carries none of
    them: the file was parsed in full and provably contained no usage (an
    abandoned or response-less session). Both carry the same identity +
    fingerprint fields.

    The no-ledger shape exists for convergence, not tidiness. Without it those
    files are re-parsed on EVERY scan forever, so a corpus whose ledger-less
    files alone outcost the caller's 250ms budget can never reach a complete
    pass — the cache warms and the scan still expires, permanently. Pinned by
    ``test_uncacheable_rollouts_do_not_block_convergence``.

    **The ledger shape stores observed CUMULATIVE STATES, not a total.** A
    rollout is not the unit of accounting: 195 ``turn_id`` values span 244 of
    746 files on a real corpus (fork / retry / resume), sharing 85% of their
    ledger before diverging. Per-file totals therefore double-count roughly
    half the corpus. ``states`` keeps every distinct ``(turn, cumulative)``
    pair this file observed so ``_aggregate`` can union them across files and
    difference the union once. See ``_aggregate``.

    Entries written before this Track carried ``day`` / ``model`` / ``usage``
    instead. The ABSENCE of ``states`` is the version discriminator and forces
    one full re-walk of that file — deliberately NOT a ``CACHE_VERSION`` bump,
    which shares a constant with the Grok namespace and would discard it
    too. Same call this repo made twice already, for
    ``skills_by_day`` (v0.11.27) and ``offset``/``head`` (v0.12.15), both times
    because a bump throws away valid data that is expensive to rebuild.

    The four resume fields exist because a bucket alone cannot continue a
    walk: ``last_total`` is what an appended ledger must be differenced
    AGAINST, ``last_model`` / ``last_turn`` are the attribution context a
    resumed segment inherits, and ``pending`` holds ledgers observed before any
    ``turn_context`` — which a segment boundary can otherwise strand.

    **String columns stay interned because this graph is large.** Track 63A
    measured 84,910 states across 1,066 rollouts on device 3a6c7dc9,
    2026-09-17, Python 3.14.7. Compact JSON was 4,040,684 bytes; the prior
    1,053-entry cache was 15,870,126 bytes indented / 4,027,507 compact.
    Warm reads took 166.66–168.26 ms before serialization. The separate
    encoding follow-up's 25 MB trigger now refers to compact bytes; see
    docs/invariants/events-retro.md for the phase split and interpreter.
    """

    dev: int
    ino: int
    size: int
    mtime_ns: int
    head: str
    head_len: int
    tail: str
    tail_len: int
    offset: int
    turn_ids: list[str]
    days: list[str]
    models: list[str]
    states: list[list[Any]]
    last_total: list[int]
    last_model: str
    last_turn: str
    pending: list[list[Any]]
    no_ledger: bool


@host_usage._pause_gc()
def read_codex_usage(
    root: Path | None = None,
    *,
    deadline: float | None = None,
) -> host_usage.HostUsageResult:
    """Read complete Codex rollout totals, using the isolated local cache.

    ``deadline`` is an absolute ``time.monotonic()`` deadline. The reader
    checks it before discovery, per file, per bounded line, and persistence.
    Cache contention is a single non-blocking attempt, never the normal
    750ms locked-json retry budget.

    The default pass also reads ``CODEX_ARCHIVED_SESSIONS_PATH``, with one
    completeness verdict over both roots. An explicit ``root`` reads that
    root alone.
    """
    started = time.monotonic()
    source_root = root if root is not None else CODEX_SESSIONS_PATH
    archived_root = CODEX_ARCHIVED_SESSIONS_PATH if root is None else None
    read_deadline = deadline if deadline is not None else started + host_usage.DEFAULT_READ_BUDGET_S
    if host_usage._expired(read_deadline):
        return host_usage._incomplete("deadline")

    try:
        with locked_json_rmw(
            CACHE_PATH,
            mode=0o600,
            compact=True,
            default_factory=_empty_cache,
            retry_intervals=(),
            on_contention="raise" if host_usage._DIAGNOSTIC.get() is not None else "warn",
            contention_warning="host token cache was locked; skipping host usage scan",
        ) as locked:
            if not locked.is_locked:
                return host_usage._incomplete("locked")
            prior = (
                host_usage._cached_last_reason(locked.data),
                host_usage._cached_reason_since(locked.data),
            )
            cached_files = host_usage._cached_files(locked.data)
            if host_usage._expired(read_deadline):
                result, staged_files, learned = host_usage._incomplete("deadline"), {}, False
            else:
                result, staged_files, learned = _scan_codex_root(
                    source_root, cached_files, read_deadline, archived_root=archived_root
                )
            ready = time.monotonic()
            over_budget = result.complete and host_usage._expired(read_deadline)
            now = datetime.now(timezone.utc)
            carried = host_usage._carry_reason(*prior, result, now, over_budget=over_budget)
            timing = host_usage._carry_read_timing(
                locked.data,
                result,
                carried[0],
                started,
                ready,
                read_deadline,
                now,
                over_budget=over_budget,
            )
            if host_usage._skip_failed_cache_write(
                learned, result, prior, carried, locked.data, timing
            ):
                # Cache hits are staged too. Only newly learned files, a
                # changed (reason, since) pair, or changed timing evidence
                # justify rewriting a failed pass. The pair comparison dates
                # a migrated blocker exactly once.
                raise host_usage._NoCacheCommit(result)
            # Cache persistence is DECOUPLED from result validity. Whether the
            # scan may be published is one question; whether we learned
            # something durable about individual files is another. Conflating
            # them is what made a large corpus unable to bootstrap: every
            # bounded scan re-parsed the same prefix, hit the deadline in the
            # same place, and discarded it, so attempt 100 stood exactly where
            # attempt 1 did. Measured on a 452-rollout Mac: six consecutive
            # bounded scans, zero bytes cached.
            locked.data = {
                **timing,
                "version": host_usage.CACHE_VERSION,
                "last_reason": carried[0],
                "last_reason_since": carried[1],
                # A complete pass observed every rollout on disk, so REPLACING
                # the map is what prunes entries for deleted files. A partial
                # pass must MERGE: replacing would delete the entries for every
                # file it never reached, and the cache would thrash between
                # prefixes instead of converging. Entries for files deleted
                # during a run of partial passes survive until the next
                # complete pass prunes them; they are inert either way, because
                # every entry is revalidated against dev/ino/size/mtime and a
                # head+tail fingerprint before it is trusted.
                "files": staged_files if result.complete else {**cached_files, **staged_files},
            }
            if over_budget:
                # The scan finished but overran its budget: refuse to publish
                # (unchanged), yet keep the cache above so the work counts.
                result = host_usage._incomplete("deadline")
        host_usage._notice_cache_write_failure(locked.write_error)
        return result
    except LockContended:
        host_usage._diagnostic(
            "mm: notice: host token cache was locked; skipping host usage scan\n", False
        )
        return host_usage._incomplete("locked")
    except host_usage._NoCacheCommit as aborted:
        return aborted.result
    except OSError:
        return host_usage._incomplete("io_error")


def codex_usage_diag() -> dict[str, Any]:
    """On-disk Codex usage-reader state. Does not open the host store.

    Exists because the per-turn migration is otherwise undiagnosable. It forces
    one full re-walk of every rollout (absence of ``states`` is the version
    discriminator), which an autopush-only Mac completes over several bounded
    passes with nothing on any surface saying so. Before this, ``mm diag``'s
    ``host_usage`` block had exactly one key, ``grok``.

    Cache-only, like its Grok sibling: ``mm diag`` must run without a
    passphrase and without a valid config, so absence, lock contention, and
    unreadable files are reported as ``cache_state``, never raised.

    ``migrating`` is the state a user needs to recognise: some entries are
    already per-turn and some are still pre-Track, so the published Codex
    numbers are incomplete but converging. ``pending`` counts the rollouts on
    disk that no cache entry covers yet.
    """
    blank = {
        **host_usage._cached_read_timing({}),
        "cache_state": "missing",
        "state": "cold",
        "files_cached": 0,
        "files_migrated": 0,
        "files_pre_track": 0,
        "files_on_disk": None,
        "pending": None,
        "model_count": 0,
        "models": [],
        "last_reason": None,
        "last_reason_since": None,
    }
    try:
        with locked_json_snapshot(CACHE_PATH) as snap:
            data = snap.data
            state = snap.state
    except OSError:
        return {**blank, "cache_state": "unreadable"}
    if state != "valid" or not isinstance(data, dict):
        return {
            **blank,
            "cache_state": "missing" if state in {"missing", "empty"} else "unreadable",
        }
    files = host_usage._cached_files(data)
    migrated = 0
    pre_track = 0
    for value in files.values():
        if not isinstance(value, dict):
            continue
        if value.get("no_ledger") is True or isinstance(value.get("states"), list):
            migrated += 1
        else:
            pre_track += 1
    on_disk: int | None
    try:
        on_disk = sum(
            1 for path in CODEX_SESSIONS_PATH.rglob("*") if _ROLLOUT_NAME.fullmatch(path.name)
        )
        # Same predicates as `_iter_archived_rollouts`, without its deadline.
        # Without this, cached archived entries exceed the count and `pending`
        # clamps to 0.
        archive = CODEX_ARCHIVED_SESSIONS_PATH
        if _archive_root_exists(archive):
            on_disk += sum(
                1
                for path in archive.iterdir()
                if _ROLLOUT_NAME.fullmatch(path.name)
                and host_usage._is_regular_non_symlink(path, missing_ok=False)
            )
    except (OSError, host_usage._ReadFailure):
        on_disk = None
    cached = migrated + pre_track
    if not cached:
        phase = "cold"
    elif pre_track:
        phase = "migrating"
    elif on_disk is not None and cached < on_disk:
        phase = "migrating"
    else:
        phase = "ready"
    models = _diag_model_ids(files.values())
    return {
        "cache_state": "ok",
        "state": phase,
        "files_cached": cached,
        "files_migrated": migrated,
        "files_pre_track": pre_track,
        "files_on_disk": on_disk,
        "pending": None if on_disk is None else max(0, on_disk - cached),
        "model_count": models["model_count"],
        "models": models["models"],
        **host_usage._cached_read_timing(data),
        "last_reason": host_usage._cached_last_reason(data),
        "last_reason_since": host_usage._cached_reason_since(data),
    }


def _diag_model_ids(entries: Any) -> dict[str, Any]:
    """Distinct model ids interned on Codex cache entries. Cache-only.

    Reads the ``models`` string table ``_cache_entry`` already writes, which
    is why this field costs no re-walk.
    """
    found: set[str] = set()
    for value in entries:
        if not isinstance(value, dict):
            continue
        models = value.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            if isinstance(model, str) and model:
                found.add(model)
    cleaned = sorted(found)
    return {"model_count": len(cleaned), "models": cleaned[: host_usage._DIAG_MODEL_CAP]}


def _scan_codex_root(
    root: Path,
    cached_files: dict[str, Any],
    deadline: float,
    *,
    archived_root: Path | None = None,
) -> tuple[host_usage.HostUsageResult, dict[str, _CacheEntry], bool]:
    """Returns ``(result, staged, learned)``.

    ``learned`` is True only when at least one entry was NEWLY computed. Cache
    HITS are staged too, so a non-empty ``staged`` does not mean the scan
    discovered anything — on a fully-warm machine with one permanently
    unreadable rollout, committing on ``staged`` alone would rewrite the entire
    cache with byte-identical content on every single push.

    ``archived_root`` joins the same pass: one staged map, one verdict. The
    pass is complete only when both roots are, because the complete-pass
    REPLACE would otherwise prune the unfinished root's entries. A rollout
    moved between paths needs no dedup here; ``_aggregate``'s lineage keys
    already collapse it. If the archive exists at scan start, validate after
    archive discovery that no new live path appeared; an unarchive between
    inventories must not silently shrink the complete snapshot.
    """
    try:
        if host_usage._expired(deadline):
            raise host_usage._ReadFailure("deadline")
        archive_was_present = archived_root is not None and _archive_root_exists(archived_root)
        rollouts = [(path, root) for path in _iter_rollouts(root, deadline)]
        if archived_root is not None:
            live_paths = {path for path, _ in rollouts}
            rollouts += [
                (path, archived_root) for path in _iter_archived_rollouts(archived_root, deadline)
            ]
            # A reverse move before archive discovery can miss both listings.
            # Revalidate the live inventory under the same deadline; removals
            # are allowed for forward moves, but a new path requires a retry.
            if archive_was_present and any(
                path not in live_paths for path in _iter_rollouts(root, deadline)
            ):
                raise host_usage._ReadFailure("io_error")
    except host_usage._ReadFailure as failure:
        return host_usage._incomplete(failure.reason), {}, False

    canonical_roots = {root: root.resolve()}
    if archived_root is not None:
        canonical_roots[archived_root] = archived_root.resolve()
    # Listed live-first, so a rollout archived between the two listings has
    # its name here; see the vanished-live-path skip below.
    archived_names = {path.name for path, base in rollouts if base is archived_root}
    staged: dict[str, _CacheEntry] = {}
    learned = False
    for path, base in rollouts:
        if host_usage._expired(deadline):
            return host_usage._incomplete("deadline"), staged, learned
        key = host_usage._cache_key(path, root=base, canonical_root=canonical_roots[base])
        try:
            try:
                before = host_usage._regular_stat(path)
            except host_usage._ReadFailure as failure:
                if (
                    base is root
                    and isinstance(failure.__cause__, FileNotFoundError)
                    and path.name in archived_names
                ):
                    # Codex archived it after the live listing. The archived
                    # copy is read in this pass, so the move is not a failure.
                    # Any other stat error, or an archived path that vanished,
                    # stays io_error.
                    continue
                raise
            existing = _validated_entry(cached_files.get(key))
            entry = _cache_hit(path, before, existing, deadline)
            if entry is None:  # cache miss
                resume = _resumable_entry(path, before, existing, deadline)
                entry = (
                    _resume_rollout(path, before, resume, deadline)
                    if resume is not None
                    else _read_full_rollout(path, before, deadline)
                )
                learned = True
            staged[key] = entry
        except host_usage._ReadFailure as failure:
            # Hand back what was staged BEFORE the failure. Each entry is a
            # complete, fingerprinted parse of one stable file, so it stays
            # valid regardless of what a later file did. The result is still
            # incomplete — the caller publishes nothing — but the work is not
            # thrown away. See `read_codex_usage` for why that matters.
            return host_usage._incomplete(failure.reason), staged, learned

    try:
        buckets = host_usage._aggregate(staged.values())
    except host_usage._ReadFailure as failure:
        return host_usage._incomplete(failure.reason), staged, learned
    return (
        host_usage._result_from_buckets(
            buckets, partial_days=frozenset(buckets.unattributable_days)
        ),
        staged,
        learned,
    )


def _iter_rollouts(root: Path, deadline: float):
    """Yield only regular non-symlink date-nested Codex rollout files."""
    if host_usage._expired(deadline):
        raise host_usage._ReadFailure("deadline")
    try:
        if not root.exists():
            return
        for year in host_usage._sorted_children(root, deadline):
            if not host_usage._YEAR_PART.fullmatch(year.name) or not host_usage._is_directory(year):
                continue
            for month in host_usage._sorted_children(year, deadline):
                if not host_usage._MONTH_OR_DAY_PART.fullmatch(
                    month.name
                ) or not host_usage._is_directory(month):
                    continue
                for day in host_usage._sorted_children(month, deadline):
                    if not host_usage._MONTH_OR_DAY_PART.fullmatch(
                        day.name
                    ) or not host_usage._is_directory(day):
                        continue
                    for candidate in host_usage._sorted_children(day, deadline):
                        if not _ROLLOUT_NAME.fullmatch(candidate.name):
                            continue
                        if host_usage._is_regular_non_symlink(candidate):
                            yield candidate
    except OSError as exc:
        raise host_usage._ReadFailure("io_error") from exc


def _archive_root_exists(root: Path) -> bool:
    """Only a missing archive is empty; root I/O errors must remain visible.

    ``Path.exists()`` suppresses every OSError on Python 3.14. ``stat()`` also
    preserves the allowed root-symlink behavior without hiding those failures.
    """
    try:
        root.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise host_usage._ReadFailure("io_error") from exc
    return True


def _iter_archived_rollouts(root: Path, deadline: float):
    """Yield regular non-symlink rollouts directly under Codex's flat archive.

    Census (Codex 0.160.0): every archived rollout sits flat in the root.
    Subdirectories are ignored, so a future nested layout would be missed;
    re-check the census on a Codex upgrade (events-retro.md).
    """
    if host_usage._expired(deadline):
        raise host_usage._ReadFailure("deadline")
    try:
        if not _archive_root_exists(root):
            return
        for candidate in host_usage._sorted_children(root, deadline):
            if host_usage._expired(deadline):
                raise host_usage._ReadFailure("deadline")
            if _ROLLOUT_NAME.fullmatch(candidate.name) and host_usage._is_regular_non_symlink(
                candidate, missing_ok=False
            ):
                yield candidate
    except OSError as exc:
        raise host_usage._ReadFailure("io_error") from exc


def _read_full_rollout(path: Path, before: os.stat_result, deadline: float) -> _CacheEntry:
    """Return this rollout's cache entry — ledger or no-ledger.

    A rollout with no usage ledger still gets an entry, so an unchanged one
    costs a stat + fingerprint on later scans instead of a full re-parse. It
    must NOT be given a synthetic model or day: ``_aggregate`` would fabricate
    a family bucket out of it. See ``_CacheEntry``.
    """
    walk = _read_rollout(path, 0, None, before, deadline)
    after = host_usage._regular_stat(path)
    if not host_usage._same_source(before, after):
        raise host_usage._ReadFailure("stale")
    fingerprint = host_usage._fingerprint(path, after, deadline)
    if not host_usage._same_source(after, host_usage._regular_stat(path)):
        raise host_usage._ReadFailure("stale")
    if walk is None:
        return _no_ledger_entry(after, fingerprint)
    return _cache_entry(after, fingerprint, walk)


def _cache_hit(
    path: Path,
    before: os.stat_result,
    entry: _CacheEntry | None,
    deadline: float,
) -> _CacheEntry | None:
    if entry is None:
        return None
    if not _same_cache_metadata(entry, before):
        return None
    fingerprint = host_usage._fingerprint(path, before, deadline)
    if not _same_fingerprint(entry, fingerprint):
        return None
    if not host_usage._same_source(before, host_usage._regular_stat(path)):
        raise host_usage._ReadFailure("stale")
    return entry


def _resumable_entry(
    path: Path,
    source: os.stat_result,
    entry: _CacheEntry | None,
    deadline: float,
) -> _CacheEntry | None:
    """Return a prior entry only when this is a verified append.

    The prior head and the bytes that were the old tail must still match.
    This catches common same-path rewrites before using the complete-line
    offset. Any doubt falls back to a bounded full parse.
    """
    if entry is None:
        return None
    if entry.get("no_ledger"):
        # A no-ledger entry cannot seed a resume: it has no terminal to carry
        # forward AND no remembered `turn_context` model. Resuming from its
        # offset would meet the file's first ledger with `current_model=None`,
        # which `_read_rollout` then refuses as unattributable — turning a file
        # that just gained its first response into a whole-store refusal. Fall
        # back to a full parse, which re-reads the model context.
        return None
    if entry["dev"] != source.st_dev or entry["ino"] != source.st_ino:
        return None
    if source.st_size <= entry["size"]:
        return None
    if entry["head_len"] != min(entry["size"], host_usage._HEAD_PROBE_BYTES) or entry[
        "tail_len"
    ] != min(entry["size"], host_usage._TAIL_PROBE_BYTES):
        return None
    if host_usage._digest_range(path, 0, entry["head_len"], deadline) != entry["head"]:
        return None
    old_tail_start = entry["size"] - entry["tail_len"]
    if host_usage._digest_range(path, old_tail_start, entry["tail_len"], deadline) != entry["tail"]:
        return None
    if not host_usage._same_source(source, host_usage._regular_stat(path)):
        raise host_usage._ReadFailure("stale")
    return entry


def _resume_rollout(
    path: Path,
    before: os.stat_result,
    entry: _CacheEntry,
    deadline: float,
) -> _CacheEntry:
    walk = _read_rollout(path, entry["offset"], _walk_from_entry(entry), before, deadline)
    if walk is None:
        # Unreachable today: the carried walk already holds this file's states,
        # so a resumed read always returns them. Guarded rather than asserted
        # so a future change to `_read_rollout` degrades to a bounded refusal
        # instead of a TypeError inside `_cache_entry`.
        raise host_usage._ReadFailure("unsupported")
    after = host_usage._regular_stat(path)
    if not host_usage._same_source(before, after):
        raise host_usage._ReadFailure("stale")
    fingerprint = host_usage._fingerprint(path, after, deadline)
    if not host_usage._same_source(after, host_usage._regular_stat(path)):
        raise host_usage._ReadFailure("stale")
    return _cache_entry(after, fingerprint, walk)


def _walk_from_entry(entry: _CacheEntry) -> _Walk:
    """Rehydrate the carried walk state of an earlier segment.

    Every field matters. Dropping ``states`` would lose this file's share of a
    turn that other files also observed; dropping ``last_total`` would make the
    first appended ledger difference against nothing; dropping ``last_model`` /
    ``last_turn`` would meet it with no attribution and refuse the whole store;
    dropping ``pending`` would strand ledgers whose ``turn_context`` had not
    arrived when the previous segment ended.
    """
    walk = _Walk(
        states=[host_usage._TurnState(*parts) for parts in host_usage._entry_states(entry)],
        pending=[
            (tuple(total), day, (tuple(last) if last is not None else None))
            for total, day, last in (entry.get("pending") or ())
        ],
        turn=entry.get("last_turn", ""),
    )
    last_total = entry.get("last_total")
    if last_total is not None:
        walk.last_total = tuple(last_total)
    model = entry.get("last_model")
    if model:
        walk.model = model
    return walk


def _read_rollout(
    path: Path,
    start_offset: int,
    previous: _Walk | None,
    before: os.stat_result,
    deadline: float,
) -> _Walk | None:
    """Read a stable file from a complete-line offset, collecting turn states.

    Returns ``None`` when the rollout recorded no usage ledger at all — an
    abandoned or response-less session contributes nothing, which is a fact
    about that file rather than a reason to refuse the whole store. Refusal is
    reserved for a ledger we saw but could not attribute (see below).

    ``previous`` is the carried walk state of an earlier segment of the SAME
    file (see ``_resume_rollout``). It supplies the cumulative baseline the
    first appended ledger is differenced against, the inherited model and turn,
    and any pre-``turn_context`` ledgers that segment could not yet attribute.
    """
    walk = previous if previous is not None else _Walk()
    current_model = walk.model
    saw_usage_ledger = previous is not None and bool(walk.states)
    last_offset = start_offset
    try:
        with path.open("rb") as fp:
            fp.seek(start_offset)
            for raw, end_offset in iter_bounded_lines(
                fp,
                str(path),
                start_offset,
                label="host usage walker",
            ):
                if host_usage._expired(deadline):
                    raise host_usage._ReadFailure("deadline")
                last_offset = end_offset
                if raw == b"":
                    raise host_usage._ReadFailure("malformed")
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError, UnicodeDecodeError) as exc:
                    raise host_usage._ReadFailure("malformed") from exc
                if not isinstance(record, dict):
                    raise host_usage._ReadFailure("malformed")
                record_type = record.get("type")
                if record_type == "turn_context":
                    current_model = _context_model(record)
                    walk.model = current_model
                    walk.turn = _context_turn(record)
                    # A `turn_context` is the first thing that can attribute a
                    # pre-context ledger, so flush the buffer HERE. Under the
                    # cumulative reading this buffer did not exist: the code
                    # dropped those records and a comment justified it with
                    # "totals are CUMULATIVE, so a later attributable record
                    # restates these tokens". Per-turn accounting deletes that
                    # premise, and on a real corpus the dropped prefix is
                    # 1,557 records across 7 rollouts worth 209,515,399 input
                    # tokens. Dropping them now would be silent loss.
                    _flush_pending(walk)
                    continue
                if record_type == "event_msg" and _is_token_count(record):
                    if not _carries_usage(record):
                        # Codex emits a `token_count` whose `payload.info` is
                        # null at the start of a turn, before the model has
                        # reported anything. It is a marker with no ledger
                        # attached, not a malformed record — 33% of rollouts
                        # on a real Codex machine carry one, and treating it
                        # as fatal refused the entire store.
                        continue
                    saw_usage_ledger = True
                    total, day, last = _reading_from_record(record)
                    if current_model is None:
                        # Not attributable YET. Buffer rather than drop; the
                        # next `turn_context` flushes it. If none ever arrives
                        # the file is refused below, never silently zeroed.
                        walk.pending.append((total, day, last))
                        continue
                    walk.states.append(
                        host_usage._TurnState(walk.turn, total, day, current_model, last)
                    )
                    walk.last_total = total
            if fp.tell() != last_offset:
                # `iter_bounded_lines` intentionally leaves a trailing
                # unterminated write behind the persisted offset.
                raise host_usage._ReadFailure("partial")
    except OSError as exc:
        raise host_usage._ReadFailure("io_error") from exc

    if not walk.states and walk.pending:
        # We read real token counts and could not attribute a single one to a
        # model. That is a shape this reader does not understand, and silently
        # dropping it would under-report usage — refuse the store instead.
        # NOTE the buffer must NOT rescue this case: a file whose only ledgers
        # precede every `turn_context` is exactly the shape
        # `test_missing_model_before_token_is_incomplete` pins as fatal.
        raise host_usage._ReadFailure("unsupported")
    if not walk.states and saw_usage_ledger:
        raise host_usage._ReadFailure("unsupported")
    if not host_usage._same_source(before, host_usage._regular_stat(path)):
        raise host_usage._ReadFailure("stale")
    return walk if walk.states else None


@dataclass
class _Walk:
    """Mutable walk state for ONE rollout, carried across segment boundaries.

    Mutable on purpose: it is threaded through a resume, persisted to the cache
    entry, and rehydrated. These fields are exactly what a resumed segment
    cannot reconstruct from the appended bytes alone.
    """

    states: list[host_usage._TurnState] = field(default_factory=list)
    pending: list[tuple[tuple[int, ...], str, tuple[int, ...] | None]] = field(default_factory=list)
    last_total: tuple[int, ...] | None = None
    model: str | None = None
    turn: str = ""


def _flush_pending(walk: _Walk) -> None:
    """Attribute buffered pre-``turn_context`` ledgers to the now-known model.

    Called only from the ``turn_context`` branch, so ``walk.model`` is set.
    Attributing an EARLIER ledger to a LATER context is normally forbidden
    here — `test_model_context_after_token_is_not_retroactively_used` pins
    that — but this case is different in kind and the difference is why the
    buffer is safe: those records had no candidate model at all, and the
    established stance for an unattributable ledger (see
    `test_missing_model_before_token_is_incomplete`) is to refuse, never to
    drop. Attributing to the first model the file names is the only reading
    that neither refuses a routine shape nor loses real tokens.
    """
    if not walk.pending or walk.model is None:
        return
    for total, day, last in walk.pending:
        walk.states.append(host_usage._TurnState(walk.turn, total, day, walk.model, last))
        walk.last_total = total
    walk.pending.clear()


def _reading_from_record(
    record: dict[str, Any],
) -> tuple[tuple[int, ...], str, tuple[int, ...] | None]:
    """Extract ``(cumulative_total, utc_day, last_token_usage)`` from a ledger.

    Both counter maps go through `_counter`, so the required/optional split and
    the `_MAX_COUNTER` bound still apply per record. `last_token_usage` is
    optional at this layer: every record on a real corpus carries it, but the
    first-state rule tolerates its absence by falling back to the cumulative,
    which is correct for a rollout that inherited nothing.
    """
    payload = record.get("payload")
    info = payload.get("info") if isinstance(payload, dict) else None
    totals = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(totals, dict):
        raise host_usage._ReadFailure("unsupported")
    total = _counters(totals)
    # Deliberately validated but never summed: total_tokens omits the cache
    # counters and reasoning_output_tokens is already inside output_tokens.
    _counter(totals, "reasoning_output_tokens", required=False)
    _counter(totals, "total_tokens", required=False)
    raw_last = info.get("last_token_usage") if isinstance(info, dict) else None
    last = _counters(raw_last) if isinstance(raw_last, dict) else None
    return total, _record_day(record), last


def _counters(totals: dict[str, Any]) -> tuple[int, ...]:
    """Raw inclusive Codex cumulative reading, in ``TOKEN_FIELDS`` order.

    Do NOT normalize here. These tuples are the host's own running
    counter and the transition identity ``(previous, total)``. Inclusive
    → disjoint conversion happens on the INCREMENT in ``_aggregate``,
    after differencing, so a warm cache of pre-35A inclusive cumulatives
    still differences correctly against a new reading.
    """
    return (
        _counter(totals, "input_tokens", required=True),
        _counter(totals, "cache_write_input_tokens", required=False),
        _counter(totals, "cached_input_tokens", required=True),
        _counter(totals, "output_tokens", required=True),
    )


def _record_day(record: dict[str, Any]) -> str:
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        raise host_usage._ReadFailure("unsupported")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise host_usage._ReadFailure("unsupported") from exc
    if parsed.tzinfo is None:
        raise host_usage._ReadFailure("unsupported")
    return parsed.astimezone(timezone.utc).date().isoformat()


def _context_turn(record: dict[str, Any]) -> str:
    """Return ``turn_context.turn_id``, or ``""`` when the host omits it.

    This is the Codex analogue of Claude's ``message.id`` (``tail_msg_ids``)
    and Grok's ``_grok_terminal_key``: the identity that makes the same work
    recognisable in two different files. Present on all 2,101 ``turn_context``
    records of a real corpus. An empty id degrades to per-file accounting for
    that turn rather than refusing, because a missing OPTIONAL identity is not
    evidence of a malformed ledger.
    """
    payload = record.get("payload")
    turn = payload.get("turn_id") if isinstance(payload, dict) else None
    if not isinstance(turn, str) or not turn:
        return ""
    if len(turn.encode("utf-8")) > host_usage._MAX_PROMPT_ID_BYTES:
        raise host_usage._ReadFailure("unsupported")
    return turn


def _context_model(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    model = payload.get("model") if isinstance(payload, dict) else None
    if (
        not isinstance(model, str)
        or not model
        or len(model.encode("utf-8")) > host_usage._MAX_MODEL_ID_BYTES
    ):
        raise host_usage._ReadFailure("unsupported")
    return model


def _is_token_count(record: dict[str, Any]) -> bool:
    payload = record.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "token_count"


def _carries_usage(record: dict[str, Any]) -> bool:
    """True when a ``token_count`` event actually has a usage ledger attached.

    A null or absent ``payload.info`` is Codex's "nothing to report yet"
    marker. An ``info`` that is PRESENT but not a dict is malformed and is
    refused HERE rather than downstream: the caller's model-attribution
    buffering runs before counter validation, so a broken ledger
    arriving before the first ``turn_context`` used to slip past the refusal
    entirely. Do not widen this to "any info I can't parse is fine": the
    distinction between an empty marker and a broken ledger is the whole
    reason this reader can be trusted not to under-report.
    """
    payload = record.get("payload")
    info = payload.get("info") if isinstance(payload, dict) else None
    if info is None:
        return False
    if not isinstance(info, dict):
        raise host_usage._ReadFailure("unsupported")
    return True


def _counter(totals: dict[str, Any], key: str, *, required: bool) -> int:
    value = totals.get(key, 0)
    if key not in totals and required:
        raise host_usage._ReadFailure("unsupported")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > host_usage._MAX_COUNTER
    ):
        raise host_usage._ReadFailure("unsupported")
    return value


def _no_ledger_entry(source: os.stat_result, fingerprint: host_usage._Fingerprint) -> _CacheEntry:
    """Cache "this file was parsed in full and held no usage".

    Deliberately carries no ``day`` / ``model`` / ``usage``: ``_aggregate``
    skips it, so it can never invent a family bucket.
    """
    entry: _CacheEntry = {
        **host_usage._identity_fields(source, fingerprint),  # type: ignore[typeddict-item]
        "no_ledger": True,
    }
    return entry


def _cache_entry(
    source: os.stat_result, fingerprint: host_usage._Fingerprint, walk: _Walk
) -> _CacheEntry:
    # Interned string tables. A 1,234-state rollout repeats one 36-byte turn id
    # and one model id on every row; spelling them out cost 59 bytes a row and
    # took the on-disk cache to 23.4 MB, whose json round-trip alone is 95 ms of
    # a 250 ms autopush budget. `_aggregate` never reads `last` except on a
    # file's FIRST state, so it is stored there and nowhere else.
    turn_ids: list[str] = []
    days: list[str] = []
    models: list[str] = []

    intern_index: dict[tuple[int, str], int] = {}

    def _intern(table: list[str], value: str) -> int:
        # Dict-backed, not `list.index`: a linear scan per row is quadratic in
        # a file's distinct turn ids, and nothing bounds that count.
        key = (id(table), value)
        found = intern_index.get(key)
        if found is None:
            table.append(value)
            found = intern_index[key] = len(table) - 1
        return found

    rows: list[list[Any]] = []
    for index, state in enumerate(walk.states):
        rows.append(
            [
                _intern(turn_ids, state.turn),
                list(state.total),
                _intern(days, state.day),
                _intern(models, state.model),
                (list(state.last) if (index == 0 and state.last) else None),
            ]
        )
    entry: _CacheEntry = {
        **host_usage._identity_fields(source, fingerprint),  # type: ignore[typeddict-item]
        "turn_ids": turn_ids,
        "days": days,
        "models": models,
        "states": rows,
        "last_turn": walk.turn,
    }
    if walk.last_total is not None:
        entry["last_total"] = list(walk.last_total)
    if walk.model is not None:
        entry["last_model"] = walk.model
    if walk.pending:
        entry["pending"] = [
            [list(total), day, (list(last) if last else None)] for total, day, last in walk.pending
        ]
    return entry


def _empty_cache() -> dict[str, Any]:
    return {
        "version": host_usage.CACHE_VERSION,
        "last_reason": None,
        "last_reason_since": None,
        "files": {},
    }


def _validated_entry(value: Any) -> _CacheEntry | None:
    """Validate a cached rollout, preserving the v0.14.15 JSON contract."""
    if not isinstance(value, dict):
        return None
    for key in ("dev", "ino", "size", "mtime_ns", "head_len", "tail_len", "offset"):
        v = value.get(key)
        if type(v) is not int or v < 0:
            return None
    if value["offset"] != value["size"]:
        return None
    if not isinstance(value.get("head"), str) or not isinstance(value.get("tail"), str):
        return None
    if value.get("no_ledger") is True:
        return {**host_usage._identity_fields_from(value), "no_ledger": True}
    raw_states = value.get("states")
    if not isinstance(raw_states, list) or not raw_states:
        return None
    turn_ids = host_usage._validated_table(
        value.get("turn_ids"), host_usage._MAX_PROMPT_ID_BYTES, allow_empty=True
    )
    days = host_usage._validated_table(value.get("days"), 32)
    models = host_usage._validated_table(value.get("models"), host_usage._MAX_MODEL_ID_BYTES)
    if turn_ids is None or days is None or models is None:
        return None
    if any(not host_usage._validated_day(day) for day in days):
        return None
    nt, nd, nm = len(turn_ids), len(days), len(models)
    states = []
    append = states.append
    for raw in raw_states:
        if not isinstance(raw, list) or len(raw) != 5:
            return None
        turn, total, day, model, last = raw
        if type(turn) is not int or not 0 <= turn < nt:
            return None
        if type(day) is not int or not 0 <= day < nd:
            return None
        if type(model) is not int or not 0 <= model < nm:
            return None
        if not _counter_list_ok(total):
            return None
        if last is not None and not _counter_list_ok(last):
            return None
        append([turn, list(total), day, model, None if last is None else list(last)])
    pending = []
    for raw in value.get("pending") or ():
        parsed_pending = _validated_pending(raw)
        if parsed_pending is None:
            return None
        pending.append(parsed_pending)
    out = {
        **host_usage._identity_fields_from(value),
        "turn_ids": turn_ids,
        "days": days,
        "models": models,
        "states": states,
        "last_turn": _validated_turn_id(value.get("last_turn")),
    }
    if pending:
        out["pending"] = pending
    last_total = _validated_counter_list(value.get("last_total"))
    if last_total is not None:
        out["last_total"] = last_total
    model = value.get("last_model")
    if (
        isinstance(model, str)
        and model
        and len(model.encode("utf-8")) <= host_usage._MAX_MODEL_ID_BYTES
    ):
        out["last_model"] = model
    return out


def _validated_turn_id(value: Any) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > host_usage._MAX_PROMPT_ID_BYTES:
        return ""
    return value


def _validated_pending(raw: Any) -> list[Any] | None:
    if not isinstance(raw, list) or len(raw) != 3:
        return None
    total, day, last = raw
    counters = _validated_counter_list(total)
    if counters is None or not host_usage._validated_day(day):
        return None
    last_counters = None if last is None else _validated_counter_list(last)
    if last is not None and last_counters is None:
        return None
    return [counters, day, last_counters]


def _counter_list_ok(raw: Any) -> bool:
    # JSON-decoded counters and fresh parses contain only plain ints. bool is
    # deliberately rejected, even though it subclasses int.
    return (
        isinstance(raw, list)
        and len(raw) == len(TOKEN_FIELDS)
        and all(type(value) is int and 0 <= value <= host_usage._MAX_COUNTER for value in raw)
    )


def _validated_counter_list(raw: Any) -> list[int] | None:
    if not _counter_list_ok(raw):
        return None
    return list(raw)


def _same_cache_metadata(entry: _CacheEntry, source: os.stat_result) -> bool:
    return (
        entry["dev"] == source.st_dev
        and entry["ino"] == source.st_ino
        and entry["size"] == source.st_size
        and entry["mtime_ns"] == source.st_mtime_ns
    )


def _same_fingerprint(entry: _CacheEntry, fingerprint: host_usage._Fingerprint) -> bool:
    return (
        entry["head"] == fingerprint.head
        and entry["head_len"] == fingerprint.head_len
        and entry["tail"] == fingerprint.tail
        and entry["tail_len"] == fingerprint.tail_len
    )
