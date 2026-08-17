"""Private, local-only host-usage readers.

Track 17C supports Codex rollout logs at
``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``. Track 18D adds a consented
Grok reader for ``updates.jsonl`` terminal records under ``GROK_HOME/sessions``
(else ``~/.grok/sessions``). A rollout's last supported
``event_msg`` / ``payload.type == "token_count"`` record is cumulative, so it
*replaces* the previous total for that file; summing every token-count record
would double-count. The model is the most recent preceding
``turn_context.payload.model``. Counters map onto Mind Meld's four token
fields: ``input_tokens`` → input, ``cache_write_input_tokens`` →
cache-create, ``cached_input_tokens`` → cache-read, and ``output_tokens`` →
output. ``reasoning_output_tokens`` is already part of output and is never
added a second time.

Two ordinary Codex shapes are tolerated rather than refused, because one
unreadable file fails the WHOLE scan and the caller then publishes nothing
(Track 19A is all-or-nothing). Measured on a 452-rollout machine, refusing
them cost 167 files — 37% — and the reader returned ``unsupported`` in 5ms
having died on the first one:

* a ``token_count`` whose ``payload.info`` is null — Codex's start-of-turn
  marker, carrying no ledger (33% of rollouts had one), and
* a ledger that precedes the first ``turn_context`` and so has no model yet;
  totals are cumulative, so a later attributable record restates it.

Refusal is still correct for a ledger we saw and could NOT attribute to any
model, and for a malformed (present but non-dict) ``info``. A rollout with no
ledger at all simply contributes nothing.

The reader is read-only with respect to host logs. Its private 0600 cache,
``~/.config/mind-meld/host-tokens.json``, stores only opaque path digests,
file fingerprints, bounded model IDs, and aggregate totals—never transcript
content, raw paths, prompts, or tool output. ``complete=False`` is a safety
signal: a caller must omit the host snapshot rather than serialize a partial
or invented zero. Track 19A owns that caller policy.

Cache persistence is DECOUPLED from result validity. "May this scan be
published?" and "did we learn something durable about individual files?" are
different questions, and conflating them left a large corpus unable to
bootstrap under the caller's 250ms/500ms budget: every bounded scan re-parsed
the same prefix, expired in the same place, and discarded it — measured as six
consecutive scans and zero bytes cached. A COMPLETE pass replaces the map
(that is what prunes deleted rollouts); a PARTIAL pass MERGES, because
replacing would delete entries it never reached and pruning on a listing it
never finished would drop files that were never absent. ``warm_host_cache_inline``
is the attended-command escape hatch for the first cold scan; the bounded
callers still publish only from a bounded read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

from mind_meld.lockedjson import locked_json_rmw, locked_json_snapshot
from mind_meld.token_usage import (
    TOKEN_FIELDS,
    Usage,
    iter_bounded_lines,
    merge_usage_bucket,
    zero_model_bucket,
)

CACHE_PATH = Path.home() / ".config" / "mind-meld" / "host-tokens.json"
"""Forensic host-reader cache. Deliberately separate from Claude's cache."""

CODEX_SESSIONS_PATH = Path.home() / ".codex" / "sessions"
GROK_SESSIONS_PATH = Path.home() / ".grok" / "sessions"
OPENCODE_DATA_PATH = Path.home() / ".local" / "share" / "opencode"
GROK_CACHE_PATH = Path.home() / ".config" / "mind-meld" / "grok-host-tokens.json"
OPENCODE_CACHE_PATH = Path.home() / ".config" / "mind-meld" / "opencode-host-tokens.json"
CACHE_VERSION = 1
DEFAULT_READ_BUDGET_S = 5.0

_HEAD_PROBE_BYTES = 4096
_TAIL_PROBE_BYTES = 4096
_MAX_MODEL_ID_BYTES = 256
_MAX_PROMPT_ID_BYTES = 256
_MAX_COUNTER = 2**53
_OPENCODE_SUCCESS_FINISHES = frozenset({"content-filter", "length", "stop", "tool-calls"})
_GROK_STOPS = frozenset({"end_turn", "cancelled"})
_GROK_CONTENT_FIELDS = frozenset({"content", "rawInput", "rawOutput"})
_GROK_TERMINAL_KEYS = frozenset({"prompt_id", "sessionUpdate", "stop_reason", "usage"})
_CANONICAL_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_YEAR_PART = re.compile(r"^\d{4}$")
_MONTH_OR_DAY_PART = re.compile(r"^\d{2}$")
_ROLLOUT_NAME = re.compile(r"^rollout-.*\.jsonl$")

HostFamily = Literal["claude", "codex", "grok", "other"]
Reason = Literal[
    "busy",
    "deadline",
    "io_error",
    "locked",
    "malformed",
    "migration",
    "no_metadata_ledger",
    "partial",
    "stale",
    "unsupported",
]
"""Why a read did not complete.

``no_metadata_ledger`` is categorically different from every sibling and the
distinction is load-bearing. It means "this store, by design, exposes no
metadata-only usage ledger, so there is nothing here to read and there never
will be" — a standing property of the SOURCE. Every other reason, including
``unsupported``, means "I found data and could not safely interpret it", which
is a FAILURE and must keep its all-or-nothing veto so a caller never publishes
totals that silently omit real usage. A caller may treat
``no_metadata_ledger`` as "this source is not installed"; it must not do that
with any other reason."""
HostTokens = dict[str, dict[str, Usage]]


@dataclass(frozen=True)
class HostUsageResult:
    """Aggregate totals as ``{host_family: {UTC-day: Usage}}``.

    Empty ``hosts`` with ``complete=True`` is a real completed empty scan.
    Empty ``hosts`` with ``complete=False`` is intentionally *not* a zero.
    """

    hosts: HostTokens
    complete: bool
    reason: Reason | None = None

    @property
    def empty(self) -> bool:
        return not self.hosts


class _CacheEntry(TypedDict, total=False):
    """One rollout's cached parse. Two shapes share this map.

    A LEDGER entry carries ``day`` / ``model`` / ``usage``. A NO-LEDGER entry
    sets ``no_ledger`` instead and carries none of them: the file was parsed in
    full and provably contained no usage (an abandoned or response-less
    session). Both carry the same identity + fingerprint fields.

    The no-ledger shape exists for convergence, not tidiness. Without it those
    files are re-parsed on EVERY scan forever, so a corpus whose ledger-less
    files alone outcost the caller's 250ms budget can never reach a complete
    pass — the cache warms and the scan still expires, permanently. Pinned by
    ``test_uncacheable_rollouts_do_not_block_convergence``.
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
    day: str
    model: str
    usage: Usage
    no_ledger: bool


@dataclass(frozen=True)
class _Fingerprint:
    head: str
    head_len: int
    tail: str
    tail_len: int


@dataclass(frozen=True)
class _Terminal:
    day: str
    model: str
    usage: Usage


class _ReadFailure(RuntimeError):
    def __init__(self, reason: Reason) -> None:
        self.reason = reason


class _NoCacheCommit(RuntimeError):
    """Escape a locked-json context without its unconditional write."""

    def __init__(self, result: HostUsageResult) -> None:
        self.result = result


def host_family(model: str) -> HostFamily:
    """Return Mind Meld's canonical model-family bucket.

    OpenCode's later reader must pass its model ID here; OpenCode is not a
    row of its own. Classification is case-insensitive and intentionally
    small so renderers never grow their own incompatible predicates.
    """
    normalized = model.casefold() if isinstance(model, str) else ""
    if normalized.startswith("claude-"):
        return "claude"
    if normalized.startswith("gpt-") or normalized in {"o1", "o3"} or normalized.startswith("o4-"):
        return "codex"
    if normalized.startswith("grok-"):
        return "grok"
    return "other"


def read_codex_usage(
    root: Path | None = None,
    *,
    deadline: float | None = None,
) -> HostUsageResult:
    """Read complete Codex rollout totals, using the isolated local cache.

    ``deadline`` is an absolute ``time.monotonic()`` deadline. The reader
    checks it before discovery, per file, per bounded line, and persistence.
    Cache contention is a single non-blocking attempt, never the normal
    750ms locked-json retry budget.
    """
    source_root = root if root is not None else CODEX_SESSIONS_PATH
    read_deadline = deadline if deadline is not None else time.monotonic() + DEFAULT_READ_BUDGET_S
    if _expired(read_deadline):
        return _incomplete("deadline")

    try:
        with locked_json_rmw(
            CACHE_PATH,
            mode=0o600,
            default_factory=_empty_cache,
            retry_intervals=(),
            on_contention="warn",
            contention_warning="host token cache was locked; skipping host usage scan",
        ) as locked:
            if not locked.is_locked:
                return _incomplete("locked")
            if _expired(read_deadline):
                raise _NoCacheCommit(_incomplete("deadline"))

            cached_files = _cached_files(locked.data)
            result, staged_files, learned = _scan_codex_root(
                source_root, cached_files, read_deadline
            )
            if not learned and not result.complete:
                # Nothing NEW was learned about any file — escape without
                # rewriting (and re-permissioning) the cache. Gating on
                # `learned` rather than on `staged_files` matters: cache hits
                # are staged too, so an already-warm machine with one
                # permanently unreadable rollout would otherwise pay a full
                # read/modify/write of the whole cache on every push to
                # persist content identical to what it just read.
                raise _NoCacheCommit(result)
            # Cache persistence is DECOUPLED from result validity. Whether the
            # scan may be published is one question; whether we learned
            # something durable about individual files is another. Conflating
            # them is what made a large corpus unable to bootstrap: every
            # bounded scan re-parsed the same prefix, hit the deadline in the
            # same place, and discarded it, so attempt 100 stood exactly where
            # attempt 1 did. Measured on a 452-rollout Mac: six consecutive
            # bounded scans, zero bytes cached.
            locked.data = {
                "version": CACHE_VERSION,
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
            if not result.complete:
                return result
            if _expired(read_deadline):
                # The scan finished but overran its budget: refuse to publish
                # (unchanged), yet keep the cache above so the work counts.
                return _incomplete("deadline")
            return result
    except _NoCacheCommit as aborted:
        return aborted.result
    except OSError:
        return _incomplete("io_error")


def warm_host_cache_inline(
    root: Path | None = None,
    *,
    budget_s: float = DEFAULT_READ_BUDGET_S,
    reader: str = "codex",
) -> HostUsageResult:
    """Populate the host cache under a generous one-off budget.

    The push tail's per-capture budget (250ms autopush / 500ms interactive) is
    sized for a WARM read, and a cold scan of a large corpus does not fit —
    573ms measured across 452 rollouts. Partial commits make a cold machine
    converge over a few pushes on their own; this makes it happen once,
    visibly, on an attended command instead. Mirrors
    ``token_usage.warm_token_cache_inline``.

    ``reader`` selects which incremental cache to warm. Codex and Grok have
    one; OpenCode's adapter cache stores no totals and is not warmable.

    The result is returned for tests and callers that want it, but the point is
    the side effect. Callers must still publish from a bounded capture, so this
    never becomes a back door around the explicit-deadline rule.
    """
    deadline = time.monotonic() + budget_s
    if reader == "grok":
        return read_grok_usage(root, deadline=deadline, consented=True)
    return read_codex_usage(root, deadline=deadline)


def grok_completed_once() -> bool:
    """True after a consented scan finished and saw at least one ledger file.

    Missing, corrupt, or lock-contended cache is pre-success (fail safe).
    """
    try:
        with locked_json_snapshot(GROK_CACHE_PATH) as snap:
            data = snap.data
    except OSError:
        return False
    return isinstance(data, dict) and data.get("complete_once") is True


def grok_sessions_root() -> Path:
    """Resolve the Grok sessions directory at call time."""
    env = os.environ.get("GROK_HOME")
    if env:
        return Path(env).expanduser() / "sessions"
    return GROK_SESSIONS_PATH


def read_grok_usage(
    root: Path | None = None,
    *,
    deadline: float | None = None,
    consented: bool = False,
) -> HostUsageResult:
    """Read completed Grok turn totals from ``updates.jsonl`` terminal records.

    Closed by default: ``consented=False`` does not stat or open the store.
    A caller that has the local opt-in must pass ``consented=True``.
    """
    if not consented:
        return _incomplete("no_metadata_ledger")
    source_root = root if root is not None else grok_sessions_root()
    read_deadline = deadline if deadline is not None else time.monotonic() + DEFAULT_READ_BUDGET_S
    if _expired(read_deadline):
        return _incomplete("deadline")

    try:
        with locked_json_rmw(
            GROK_CACHE_PATH,
            mode=0o600,
            default_factory=_empty_grok_cache,
            retry_intervals=(),
            on_contention="warn",
            contention_warning="host token adapter cache was locked; skipping host usage scan",
        ) as locked:
            if not locked.is_locked:
                return _incomplete("locked")
            if _expired(read_deadline):
                raise _NoCacheCommit(_incomplete("deadline"))

            cached_files = _cached_files(locked.data)
            prior_complete = locked.data.get("complete_once") is True
            result, staged_files, learned, saw_files = _scan_grok_root(
                source_root, cached_files, read_deadline
            )
            if not learned and not result.complete:
                raise _NoCacheCommit(result)
            complete_once = prior_complete or (result.complete and saw_files)
            locked.data = {
                "version": CACHE_VERSION,
                "complete_once": complete_once,
                "files": staged_files if result.complete else {**cached_files, **staged_files},
            }
            if not result.complete:
                return result
            if _expired(read_deadline):
                return _incomplete("deadline")
            return result
    except _NoCacheCommit as aborted:
        return aborted.result
    except OSError:
        return _incomplete("io_error")


def read_opencode_usage(
    root: Path | None = None,
    *,
    deadline: float | None = None,
) -> HostUsageResult:
    """Read completed OpenCode assistant messages from allowlisted storage.

    Modern OpenCode keeps messages in ``opencode.db``. Older installations
    used complete message JSON files, which this reader refuses because there
    is no metadata-only projection. A directory containing both forms is an
    unfinished migration and is intentionally not guessed. SQLite is opened
    read-only, query-only, with an immediate busy policy and one consistent
    read transaction. Queries project only usage metadata.
    """
    source_root = root if root is not None else OPENCODE_DATA_PATH
    read_deadline = deadline if deadline is not None else time.monotonic() + DEFAULT_READ_BUDGET_S
    return _read_with_adapter_lock(
        OPENCODE_CACHE_PATH,
        read_deadline,
        lambda: _scan_opencode_root(source_root, read_deadline),
    )


def _read_with_adapter_lock(
    cache_path: Path,
    deadline: float,
    reader: Any,
) -> HostUsageResult:
    """Give OpenCode an independent 0600 lock without sharing totals.

    OpenCode has no verified generation token, so it does not reuse Codex's
    append-only cache. The tiny cache is solely a separate lock namespace
    today. Grok has its own incremental cache in ``read_grok_usage``.
    """
    if _expired(deadline):
        return _incomplete("deadline")
    result = reader()
    if not result.complete:
        return result
    if _expired(deadline):
        return _incomplete("deadline")
    try:
        with locked_json_rmw(
            cache_path,
            mode=0o600,
            default_factory=_empty_adapter_cache,
            retry_intervals=(),
            on_contention="warn",
            contention_warning="host token adapter cache was locked; skipping host usage scan",
        ) as locked:
            if not locked.is_locked:
                return _incomplete("locked")
            if _expired(deadline):
                raise _NoCacheCommit(_incomplete("deadline"))
            locked.data = _empty_adapter_cache()
            return result
    except _NoCacheCommit as aborted:
        return aborted.result
    except OSError:
        return _incomplete("io_error")


def _empty_grok_cache() -> dict[str, Any]:
    return {"version": CACHE_VERSION, "complete_once": False, "files": {}}


def _scan_grok_root(
    root: Path,
    cached_files: dict[str, Any],
    deadline: float,
) -> tuple[HostUsageResult, dict[str, Any], bool, bool]:
    """Returns ``(result, staged, learned, saw_files)``."""
    try:
        ledgers = list(_iter_grok_ledgers(root, deadline))
    except _ReadFailure as failure:
        return _incomplete(failure.reason), {}, False, False

    staged: dict[str, Any] = {}
    learned = False
    for session_id, path in ledgers:
        if _expired(deadline):
            return _incomplete("deadline"), staged, learned, True
        key = _cache_key(path)
        try:
            before = _regular_stat(path)
            existing = cached_files.get(key)
            entry = _grok_cache_hit(path, before, existing, deadline)
            if entry is None:
                resume = _grok_resumable_entry(path, before, existing, deadline)
                entry = (
                    _resume_grok_file(path, session_id, before, resume, deadline)
                    if resume is not None
                    else _read_full_grok_file(path, session_id, before, deadline)
                )
                learned = True
            staged[key] = entry
        except _ReadFailure as failure:
            return _incomplete(failure.reason), staged, learned, True

    return (
        HostUsageResult(_aggregate_grok(staged.values()), complete=True),
        staged,
        learned,
        bool(ledgers),
    )


def _iter_grok_ledgers(root: Path, deadline: float):
    """Yield ``(session_id, updates.jsonl)`` for session dirs under ``root``."""
    if _expired(deadline):
        raise _ReadFailure("deadline")
    try:
        if not root.exists():
            return
        if root.is_symlink() or not root.is_dir():
            raise _ReadFailure("unsupported")
        for workspace in _sorted_children(root, deadline):
            if not _is_directory(workspace):
                continue
            for session in _sorted_children(workspace, deadline):
                if not _is_directory(session):
                    continue
                candidate = session / "updates.jsonl"
                if _is_regular_non_symlink(candidate):
                    yield session.name, candidate
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _grok_cache_hit(
    path: Path,
    before: os.stat_result,
    existing: Any,
    deadline: float,
) -> dict[str, Any] | None:
    entry = _validated_grok_entry(existing)
    if entry is None:
        return None
    if not _same_cache_metadata(entry, before):  # type: ignore[arg-type]
        return None
    fingerprint = _fingerprint(path, before, deadline)
    if not _same_fingerprint(entry, fingerprint):  # type: ignore[arg-type]
        return None
    if not _same_source(before, _regular_stat(path)):
        raise _ReadFailure("stale")
    return entry


def _grok_resumable_entry(
    path: Path,
    source: os.stat_result,
    existing: Any,
    deadline: float,
) -> dict[str, Any] | None:
    entry = _validated_grok_entry(existing)
    if entry is None:
        return None
    if entry["dev"] != source.st_dev or entry["ino"] != source.st_ino:
        return None
    if source.st_size <= entry["size"]:
        return None
    if entry["head_len"] != min(entry["size"], _HEAD_PROBE_BYTES) or entry["tail_len"] != min(
        entry["size"], _TAIL_PROBE_BYTES
    ):
        return None
    if _digest_range(path, 0, entry["head_len"], deadline) != entry["head"]:
        return None
    old_tail_start = entry["size"] - entry["tail_len"]
    if _digest_range(path, old_tail_start, entry["tail_len"], deadline) != entry["tail"]:
        return None
    if not _same_source(source, _regular_stat(path)):
        raise _ReadFailure("stale")
    return entry


def _read_full_grok_file(
    path: Path, session_id: str, before: os.stat_result, deadline: float
) -> dict[str, Any]:
    turns = _read_grok_file(path, session_id, 0, {}, before, deadline)
    return _grok_file_entry(path, before, deadline, turns)


def _resume_grok_file(
    path: Path,
    session_id: str,
    before: os.stat_result,
    entry: dict[str, Any],
    deadline: float,
) -> dict[str, Any]:
    prior = {turn["key"]: turn for turn in entry["turns"]}
    turns = _read_grok_file(path, session_id, entry["offset"], prior, before, deadline)
    return _grok_file_entry(path, before, deadline, turns)


def _grok_file_entry(
    path: Path, before: os.stat_result, deadline: float, turns: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    after = _regular_stat(path)
    if not _same_source(before, after):
        raise _ReadFailure("stale")
    fingerprint = _fingerprint(path, after, deadline)
    if not _same_source(after, _regular_stat(path)):
        raise _ReadFailure("stale")
    return {
        **_identity_fields(after, fingerprint),
        "turns": [turns[key] for key in sorted(turns)],
    }


def _read_grok_file(
    path: Path,
    session_id: str,
    start_offset: int,
    prior: dict[str, dict[str, Any]],
    before: os.stat_result,
    deadline: float,
) -> dict[str, dict[str, Any]]:
    turns = dict(prior)
    last_offset = start_offset
    try:
        with path.open("rb") as fp:
            fp.seek(start_offset)
            for raw, end_offset in iter_bounded_lines(
                fp,
                _cache_key(path),
                start_offset,
                label="grok usage walker",
            ):
                if _expired(deadline):
                    raise _ReadFailure("deadline")
                last_offset = end_offset
                if raw == b"":
                    raise _ReadFailure("malformed")
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError, UnicodeDecodeError) as exc:
                    raise _ReadFailure("malformed") from exc
                accepted = _grok_turns_from_record(record, session_id)
                if accepted is None:
                    continue
                for key, turn in accepted:
                    existing = turns.get(key)
                    if existing is None:
                        turns[key] = turn
                        continue
                    if existing == turn:
                        continue
                    raise _ReadFailure("unsupported")
            if fp.tell() != last_offset:
                raise _ReadFailure("partial")
    except OSError as exc:
        raise _ReadFailure("io_error") from exc
    if not _same_source(before, _regular_stat(path)):
        raise _ReadFailure("stale")
    return turns


def _grok_turns_from_record(
    record: Any, session_id: str
) -> list[tuple[str, dict[str, Any]]] | None:
    if not isinstance(record, dict):
        raise _ReadFailure("malformed")
    params = record.get("params")
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    if update.get("sessionUpdate") != "turn_completed":
        return None
    if _GROK_CONTENT_FIELDS & update.keys():
        return None
    if set(update) != _GROK_TERMINAL_KEYS:
        raise _ReadFailure("unsupported")
    day = _grok_outer_day(record.get("timestamp"))
    prompt_id = update.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise _ReadFailure("unsupported")
    if len(prompt_id.encode("utf-8")) > _MAX_PROMPT_ID_BYTES:
        raise _ReadFailure("unsupported")
    stop = update.get("stop_reason")
    if not isinstance(stop, str) or stop not in _GROK_STOPS:
        raise _ReadFailure("unsupported")
    usage = update.get("usage")
    if not isinstance(usage, dict):
        raise _ReadFailure("unsupported")
    models = usage.get("modelUsage")
    if not isinstance(models, dict) or not models:
        raise _ReadFailure("unsupported")
    _validate_grok_counters(usage)
    accepted: list[tuple[str, dict[str, Any]]] = []
    multi = len(models) > 1
    for model, entry in models.items():
        if not isinstance(model, str) or not model:
            raise _ReadFailure("unsupported")
        if len(model.encode("utf-8")) > _MAX_MODEL_ID_BYTES:
            raise _ReadFailure("unsupported")
        if not isinstance(entry, dict):
            raise _ReadFailure("unsupported")
        counters = _validate_grok_counters(entry)
        model_key = _grok_terminal_key(session_id, f"{prompt_id}\0{model}" if multi else prompt_id)
        accepted.append(
            (model_key, {"key": model_key, "day": day, "model": model, "usage": counters})
        )
    return accepted


def _validate_grok_counters(usage: dict[str, Any]) -> Usage:
    output = _grok_counter(usage, "outputTokens")
    reasoning = _grok_counter(usage, "reasoningTokens")
    if reasoning > output:
        raise _ReadFailure("unsupported")
    _grok_counter(usage, "totalTokens")
    return {
        "input": _grok_counter(usage, "inputTokens"),
        "cache_create": _grok_counter(usage, "cacheCreationTokens"),
        "cache_read": _grok_counter(usage, "cachedReadTokens"),
        "output": output,
    }


def _grok_counter(usage: dict[str, Any], key: str) -> int:
    if key not in usage:
        raise _ReadFailure("unsupported")
    value = usage[key]
    if not _is_valid_counter(value):
        raise _ReadFailure("unsupported")
    return value


def _grok_outer_day(value: Any) -> str:
    return _utc_day(value)


def _grok_terminal_key(session_id: str, prompt_id: str) -> str:
    return hashlib.sha256(f"{session_id}\0{prompt_id}".encode()).hexdigest()


def _validated_grok_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    integer_keys = ("dev", "ino", "size", "mtime_ns", "head_len", "tail_len", "offset")
    if any(not _is_nonnegative_int(value.get(key)) for key in integer_keys):
        return None
    if value["offset"] != value["size"]:
        return None
    if not isinstance(value.get("head"), str) or not isinstance(value.get("tail"), str):
        return None
    turns = value.get("turns")
    if not isinstance(turns, list):
        return None
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            return None
        key = turn.get("key")
        if not isinstance(key, str) or len(key) != 64 or key in seen:
            return None
        seen.add(key)
        model = turn.get("model")
        day = turn.get("day")
        usage = turn.get("usage")
        if not isinstance(model, str) or not model:
            return None
        if len(model.encode("utf-8")) > _MAX_MODEL_ID_BYTES:
            return None
        if not isinstance(day, str) or not _CANONICAL_DAY.fullmatch(day):
            return None
        if not isinstance(usage, dict):
            return None
        if any(not _is_valid_counter(usage.get(field)) for field in TOKEN_FIELDS):
            return None
        normalized.append(
            {
                "key": key,
                "day": day,
                "model": model,
                "usage": {field: usage[field] for field in TOKEN_FIELDS},
            }
        )
    return {**_identity_fields_from(value), "turns": normalized}


def _aggregate_grok(entries: Any) -> HostTokens:
    hosts: HostTokens = {}
    for entry in entries:
        for turn in entry.get("turns", ()) if isinstance(entry, dict) else ():
            family = host_family(turn["model"])
            days = hosts.setdefault(family, {})
            bucket = days.setdefault(turn["day"], zero_model_bucket())
            merge_usage_bucket(bucket, turn["usage"])
    return hosts


def _scan_opencode_root(root: Path, deadline: float) -> HostUsageResult:
    if _expired(deadline):
        return _incomplete("deadline")
    try:
        if not root.exists():
            return HostUsageResult({}, complete=True)
        if root.is_symlink():
            return _incomplete("stale")
        database = root if root.name == "opencode.db" else root / "opencode.db"
        legacy_root = (
            root.parent / "storage" / "message"
            if root.name == "opencode.db"
            else root / "storage" / "message"
        )
        has_database = database.exists()
        has_legacy = legacy_root.exists()
    except OSError:
        return _incomplete("io_error")

    if has_database and has_legacy:
        return _incomplete("migration")
    try:
        if has_database:
            terminals = _read_opencode_database(database, deadline)
        elif has_legacy:
            # Legacy message files contain complete session content. There is
            # no metadata-only projection, so do not deserialize transcripts —
            # a standing property of the source, not a failed read.
            return _incomplete("no_metadata_ledger")
        else:
            return HostUsageResult({}, complete=True)
    except _ReadFailure as failure:
        return _incomplete(failure.reason)
    return HostUsageResult(_aggregate(terminals), complete=True)


def _read_opencode_database(path: Path, deadline: float) -> list[_Terminal]:
    before = _regular_stat(path)
    connection: sqlite3.Connection | None = None
    try:
        if _expired(deadline):
            raise _ReadFailure("deadline")
        connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 0")
        connection.set_progress_handler(lambda: int(_expired(deadline)), 1000)
        connection.execute("BEGIN")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'message'"
            )
        }
        if tables != {"message"}:
            raise _ReadFailure("unsupported")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(message)")}
        if "data" not in columns:
            raise _ReadFailure("unsupported")
        invalid = connection.execute(
            "SELECT COUNT(*) FROM message WHERE data IS NULL OR NOT json_valid(data)"
        ).fetchone()
        if invalid is None or invalid[0] != 0:
            raise _ReadFailure("malformed")
        rows = connection.execute(
            """
            SELECT
                json_extract(data, '$.id'),
                json_extract(data, '$.role'),
                json_extract(data, '$.modelID'),
                json_extract(data, '$.time.completed'),
                json_extract(data, '$.error'),
                json_extract(data, '$.finish'),
                json_extract(data, '$.tokens.input'),
                json_extract(data, '$.tokens.output'),
                json_extract(data, '$.tokens.reasoning'),
                json_extract(data, '$.tokens.cache.read'),
                json_extract(data, '$.tokens.cache.write')
            FROM message
            WHERE json_extract(data, '$.role') = 'assistant'
              AND json_extract(data, '$.time.completed') IS NOT NULL
              AND json_extract(data, '$.error') IS NULL
            """
        )
        terminals: dict[str, _Terminal] = {}
        for row in rows:
            if _expired(deadline):
                raise _ReadFailure("deadline")
            (
                message_id,
                role,
                model,
                completed,
                error,
                finish,
                input_tokens,
                output,
                reasoning,
                cache_read,
                cache_create,
            ) = row
            if role != "assistant" or error is not None:
                raise _ReadFailure("malformed")
            if (
                _is_zero_opencode_ledger(
                    input_tokens,
                    output,
                    reasoning,
                    cache_read,
                    cache_create,
                )
                and finish is None
            ):
                continue
            terminal = _opencode_terminal(
                message_id,
                model,
                completed,
                finish,
                input_tokens,
                output,
                reasoning,
                cache_read,
                cache_create,
            )
            if message_id in terminals:
                raise _ReadFailure("malformed")
            terminals[message_id] = terminal
        connection.execute("COMMIT")
    except _ReadFailure:
        raise
    except sqlite3.OperationalError as exc:
        message = str(exc).casefold()
        if _expired(deadline) or "interrupted" in message:
            raise _ReadFailure("deadline") from exc
        if "busy" in message or "locked" in message:
            raise _ReadFailure("busy") from exc
        raise _ReadFailure("io_error") from exc
    except sqlite3.DatabaseError as exc:
        raise _ReadFailure("malformed") from exc
    except OSError as exc:
        raise _ReadFailure("io_error") from exc
    finally:
        if connection is not None:
            connection.close()
    if not _same_source(before, _regular_stat(path)):
        raise _ReadFailure("stale")
    return list(terminals.values())


def _opencode_terminal(
    message_id: Any,
    model: Any,
    completed: Any,
    finish: Any,
    input_tokens: Any,
    output: Any,
    reasoning: Any,
    cache_read: Any,
    cache_create: Any,
) -> _Terminal:
    if not isinstance(message_id, str) or not message_id:
        raise _ReadFailure("unsupported")
    if not isinstance(finish, str) or finish not in _OPENCODE_SUCCESS_FINISHES:
        raise _ReadFailure("unsupported")
    output_count = _single_counter(output)
    reasoning_count = _single_counter(reasoning)
    terminal = _Terminal(
        _utc_day(completed),
        _model_id({"model": model}, ("model",)),
        {
            "input": _single_counter(input_tokens),
            "cache_create": _single_counter(cache_create),
            "cache_read": _single_counter(cache_read),
            # OpenCode persists non-reasoning completion output separately;
            # Mind Meld has one output bucket, so preserve both here.
            "output": output_count + reasoning_count,
        },
    )
    if terminal.usage["output"] > _MAX_COUNTER or not any(terminal.usage.values()):
        raise _ReadFailure("unsupported")
    return terminal


def _is_zero_opencode_ledger(*values: Any) -> bool:
    return all(_is_valid_counter(value) for value in values) and not any(values)


def _single_counter(value: Any) -> int:
    if not _is_valid_counter(value):
        raise _ReadFailure("unsupported")
    return value


def _model_id(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value and len(value.encode("utf-8")) <= _MAX_MODEL_ID_BYTES:
            return value
    raise _ReadFailure("unsupported")


def _utc_day(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _ReadFailure("unsupported") from exc
        if parsed.tzinfo is None:
            raise _ReadFailure("unsupported")
        return parsed.astimezone(timezone.utc).date().isoformat()
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise _ReadFailure("unsupported")
    seconds = value / 1000 if value >= 100_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise _ReadFailure("unsupported") from exc


def _scan_codex_root(
    root: Path,
    cached_files: dict[str, Any],
    deadline: float,
) -> tuple[HostUsageResult, dict[str, _CacheEntry], bool]:
    """Returns ``(result, staged, learned)``.

    ``learned`` is True only when at least one entry was NEWLY computed. Cache
    HITS are staged too, so a non-empty ``staged`` does not mean the scan
    discovered anything — on a fully-warm machine with one permanently
    unreadable rollout, committing on ``staged`` alone would rewrite the entire
    cache with byte-identical content on every single push.
    """
    try:
        rollouts = list(_iter_rollouts(root, deadline))
    except _ReadFailure as failure:
        return _incomplete(failure.reason), {}, False

    staged: dict[str, _CacheEntry] = {}
    learned = False
    for path in rollouts:
        if _expired(deadline):
            return _incomplete("deadline"), staged, learned
        key = _cache_key(path)
        try:
            before = _regular_stat(path)
            existing = cached_files.get(key)
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
        except _ReadFailure as failure:
            # Hand back what was staged BEFORE the failure. Each entry is a
            # complete, fingerprinted parse of one stable file, so it stays
            # valid regardless of what a later file did. The result is still
            # incomplete — the caller publishes nothing — but the work is not
            # thrown away. See `read_codex_usage` for why that matters.
            return _incomplete(failure.reason), staged, learned

    return HostUsageResult(_aggregate(staged.values()), complete=True), staged, learned


def _iter_rollouts(root: Path, deadline: float):
    """Yield only regular non-symlink date-nested Codex rollout files."""
    if _expired(deadline):
        raise _ReadFailure("deadline")
    try:
        if not root.exists():
            return
        for year in _sorted_children(root, deadline):
            if not _YEAR_PART.fullmatch(year.name) or not _is_directory(year):
                continue
            for month in _sorted_children(year, deadline):
                if not _MONTH_OR_DAY_PART.fullmatch(month.name) or not _is_directory(month):
                    continue
                for day in _sorted_children(month, deadline):
                    if not _MONTH_OR_DAY_PART.fullmatch(day.name) or not _is_directory(day):
                        continue
                    for candidate in _sorted_children(day, deadline):
                        if not _ROLLOUT_NAME.fullmatch(candidate.name):
                            continue
                        if _is_regular_non_symlink(candidate):
                            yield candidate
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _sorted_children(path: Path, deadline: float) -> list[Path]:
    if _expired(deadline):
        raise _ReadFailure("deadline")
    try:
        return sorted(path.iterdir(), key=lambda child: child.name)
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _is_directory(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_dir()
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _is_regular_non_symlink(path: Path) -> bool:
    try:
        st = path.lstat()
        return not stat.S_ISLNK(st.st_mode) and stat.S_ISREG(st.st_mode)
    except OSError as exc:
        raise _ReadFailure("io_error") from exc


def _read_full_rollout(path: Path, before: os.stat_result, deadline: float) -> _CacheEntry:
    """Return this rollout's cache entry — ledger or no-ledger.

    A rollout with no usage ledger still gets an entry, so an unchanged one
    costs a stat + fingerprint on later scans instead of a full re-parse. It
    must NOT be given a synthetic model or day: ``_aggregate`` would fabricate
    a family bucket out of it. See ``_CacheEntry``.
    """
    terminal = _read_rollout(path, 0, None, before, deadline)
    after = _regular_stat(path)
    if not _same_source(before, after):
        raise _ReadFailure("stale")
    fingerprint = _fingerprint(path, after, deadline)
    if not _same_source(after, _regular_stat(path)):
        raise _ReadFailure("stale")
    if terminal is None:
        return _no_ledger_entry(after, fingerprint)
    return _cache_entry(after, fingerprint, terminal)


def _cache_hit(
    path: Path,
    before: os.stat_result,
    existing: Any,
    deadline: float,
) -> _CacheEntry | None:
    entry = _validated_entry(existing)
    if entry is None:
        return None
    if not _same_cache_metadata(entry, before):
        return None
    fingerprint = _fingerprint(path, before, deadline)
    if not _same_fingerprint(entry, fingerprint):
        return None
    if not _same_source(before, _regular_stat(path)):
        raise _ReadFailure("stale")
    return entry


def _resumable_entry(
    path: Path,
    source: os.stat_result,
    existing: Any,
    deadline: float,
) -> _CacheEntry | None:
    """Return a prior entry only when this is a verified append.

    The prior head and the bytes that were the old tail must still match.
    This catches common same-path rewrites before using the complete-line
    offset. Any doubt falls back to a bounded full parse.
    """
    entry = _validated_entry(existing)
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
    if entry["head_len"] != min(entry["size"], _HEAD_PROBE_BYTES) or entry["tail_len"] != min(
        entry["size"], _TAIL_PROBE_BYTES
    ):
        return None
    if _digest_range(path, 0, entry["head_len"], deadline) != entry["head"]:
        return None
    old_tail_start = entry["size"] - entry["tail_len"]
    if _digest_range(path, old_tail_start, entry["tail_len"], deadline) != entry["tail"]:
        return None
    if not _same_source(source, _regular_stat(path)):
        raise _ReadFailure("stale")
    return entry


def _resume_rollout(
    path: Path,
    before: os.stat_result,
    entry: _CacheEntry,
    deadline: float,
) -> _CacheEntry:
    previous = _Terminal(entry["day"], entry["model"], entry["usage"])
    terminal = _read_rollout(path, entry["offset"], previous, before, deadline)
    if terminal is None:
        # Unreachable today: `previous` seeds `terminal`, so a resumed read
        # always carries at least the cached total forward. Guarded rather
        # than asserted so a future change to `_read_rollout` degrades to a
        # bounded refusal instead of a TypeError inside `_cache_entry`.
        raise _ReadFailure("unsupported")
    after = _regular_stat(path)
    if not _same_source(before, after):
        raise _ReadFailure("stale")
    fingerprint = _fingerprint(path, after, deadline)
    if not _same_source(after, _regular_stat(path)):
        raise _ReadFailure("stale")
    return _cache_entry(after, fingerprint, terminal)


def _read_rollout(
    path: Path,
    start_offset: int,
    previous: _Terminal | None,
    before: os.stat_result,
    deadline: float,
) -> _Terminal | None:
    """Read a stable file from a complete-line offset and retain last total.

    Returns ``None`` when the rollout recorded no usage ledger at all — an
    abandoned or response-less session contributes nothing, which is a fact
    about that file rather than a reason to refuse the whole store. Refusal is
    reserved for a ledger we saw but could not attribute (see below).
    """
    current_model = previous.model if previous is not None else None
    terminal = previous
    saw_usage_ledger = previous is not None
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
                if _expired(deadline):
                    raise _ReadFailure("deadline")
                last_offset = end_offset
                if raw == b"":
                    raise _ReadFailure("malformed")
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError, UnicodeDecodeError) as exc:
                    raise _ReadFailure("malformed") from exc
                if not isinstance(record, dict):
                    raise _ReadFailure("malformed")
                record_type = record.get("type")
                if record_type == "turn_context":
                    current_model = _context_model(record)
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
                    if current_model is None:
                        # A ledger before the first `turn_context` cannot be
                        # attributed yet. Totals are CUMULATIVE, so a later
                        # attributable record restates these tokens; dropping
                        # this one loses nothing. If no `turn_context` ever
                        # arrives, `saw_usage_ledger` makes the file fatal
                        # below rather than silently discarding real usage.
                        continue
                    terminal = _terminal_from_record(record, current_model)
            if fp.tell() != last_offset:
                # `iter_bounded_lines` intentionally leaves a trailing
                # unterminated write behind the persisted offset.
                raise _ReadFailure("partial")
    except OSError as exc:
        raise _ReadFailure("io_error") from exc

    if terminal is None and saw_usage_ledger:
        # We read real token counts and could not attribute a single one to a
        # model. That is a shape this reader does not understand, and silently
        # dropping it would under-report usage — refuse the store instead.
        raise _ReadFailure("unsupported")
    if not _same_source(before, _regular_stat(path)):
        raise _ReadFailure("stale")
    return terminal


def _context_model(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    model = payload.get("model") if isinstance(payload, dict) else None
    if not isinstance(model, str) or not model or len(model.encode("utf-8")) > _MAX_MODEL_ID_BYTES:
        raise _ReadFailure("unsupported")
    return model


def _is_token_count(record: dict[str, Any]) -> bool:
    payload = record.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "token_count"


def _carries_usage(record: dict[str, Any]) -> bool:
    """True when a ``token_count`` event actually has a usage ledger attached.

    A null or absent ``payload.info`` is Codex's "nothing to report yet"
    marker. An ``info`` that is PRESENT but not a dict is malformed and is
    refused HERE rather than downstream: the caller's model-attribution
    ``continue`` runs before ``_terminal_from_record``, so a broken ledger
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
        raise _ReadFailure("unsupported")
    return True


def _terminal_from_record(record: dict[str, Any], model: str) -> _Terminal:
    payload = record.get("payload")
    info = payload.get("info") if isinstance(payload, dict) else None
    totals = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(totals, dict):
        raise _ReadFailure("unsupported")
    usage: Usage = {
        "input": _counter(totals, "input_tokens", required=True),
        "cache_create": _counter(totals, "cache_write_input_tokens", required=False),
        "cache_read": _counter(totals, "cached_input_tokens", required=True),
        "output": _counter(totals, "output_tokens", required=True),
    }
    # These fields are deliberately validated but never summed: total_tokens
    # omits cache counters and reasoning_output_tokens is inside output_tokens.
    _counter(totals, "reasoning_output_tokens", required=False)
    _counter(totals, "total_tokens", required=False)
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        raise _ReadFailure("unsupported")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _ReadFailure("unsupported") from exc
    if parsed.tzinfo is None:
        raise _ReadFailure("unsupported")
    return _Terminal(parsed.astimezone(timezone.utc).date().isoformat(), model, usage)


def _counter(totals: dict[str, Any], key: str, *, required: bool) -> int:
    value = totals.get(key, 0)
    if key not in totals and required:
        raise _ReadFailure("unsupported")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > _MAX_COUNTER:
        raise _ReadFailure("unsupported")
    return value


def _aggregate(entries: Any) -> HostTokens:
    hosts: HostTokens = {}
    for entry in entries:
        # No-ledger entries exist only to make an unchanged ledger-less file a
        # cheap cache hit. They carry no model/day/usage and must never reach
        # `host_family`, which would bucket "" as the `other` family.
        if not isinstance(entry, _Terminal) and entry.get("no_ledger"):
            continue
        model = entry.model if isinstance(entry, _Terminal) else entry["model"]
        day = entry.day if isinstance(entry, _Terminal) else entry["day"]
        usage = entry.usage if isinstance(entry, _Terminal) else entry["usage"]
        family = host_family(model)
        days = hosts.setdefault(family, {})
        bucket = days.setdefault(day, zero_model_bucket())
        merge_usage_bucket(bucket, usage)
    return hosts


_IDENTITY_KEYS = (
    "dev",
    "ino",
    "size",
    "mtime_ns",
    "head",
    "head_len",
    "tail",
    "tail_len",
    "offset",
)


def _identity_fields(source: os.stat_result, fingerprint: _Fingerprint) -> dict[str, Any]:
    return {
        "dev": source.st_dev,
        "ino": source.st_ino,
        "size": source.st_size,
        "mtime_ns": source.st_mtime_ns,
        "head": fingerprint.head,
        "head_len": fingerprint.head_len,
        "tail": fingerprint.tail,
        "tail_len": fingerprint.tail_len,
        "offset": source.st_size,
    }


def _identity_fields_from(value: dict[str, Any]) -> dict[str, Any]:
    """Project an already-validated cache dict down to identity fields only."""
    return {key: value[key] for key in _IDENTITY_KEYS}


def _no_ledger_entry(source: os.stat_result, fingerprint: _Fingerprint) -> _CacheEntry:
    """Cache "this file was parsed in full and held no usage".

    Deliberately carries no ``day`` / ``model`` / ``usage``: ``_aggregate``
    skips it, so it can never invent a family bucket.
    """
    entry: _CacheEntry = {**_identity_fields(source, fingerprint), "no_ledger": True}  # type: ignore[typeddict-item]
    return entry


def _cache_entry(
    source: os.stat_result, fingerprint: _Fingerprint, terminal: _Terminal
) -> _CacheEntry:
    return {
        "dev": source.st_dev,
        "ino": source.st_ino,
        "size": source.st_size,
        "mtime_ns": source.st_mtime_ns,
        "head": fingerprint.head,
        "head_len": fingerprint.head_len,
        "tail": fingerprint.tail,
        "tail_len": fingerprint.tail_len,
        "offset": source.st_size,
        "day": terminal.day,
        "model": terminal.model,
        "usage": dict(terminal.usage),
    }


def _cached_files(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("version") != CACHE_VERSION:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _empty_cache() -> dict[str, Any]:
    return {"version": CACHE_VERSION, "files": {}}


def _empty_adapter_cache() -> dict[str, int]:
    """Schema marker for non-incremental adapter lock files.

    Keeping this intentionally content-free means an interrupted OpenCode
    scan cannot cause the next successful scan to replay stale usage.
    """
    return {"version": CACHE_VERSION}


def _validated_entry(value: Any) -> _CacheEntry | None:
    if not isinstance(value, dict):
        return None
    integer_keys = ("dev", "ino", "size", "mtime_ns", "head_len", "tail_len", "offset")
    if any(not _is_nonnegative_int(value.get(key)) for key in integer_keys):
        return None
    if value["offset"] != value["size"]:
        return None
    if not isinstance(value.get("head"), str) or not isinstance(value.get("tail"), str):
        return None
    if value.get("no_ledger") is True:
        # Identity + fingerprint only, and normalized to exactly that: any
        # day/model/usage riding along on a no_ledger entry is dropped rather
        # than trusted, so a hand-edited cache cannot smuggle totals in behind
        # the flag `_aggregate` uses to skip it.
        entry: _CacheEntry = {
            **_identity_fields_from(value),  # type: ignore[typeddict-item]
            "no_ledger": True,
        }
        return entry
    if not isinstance(value.get("day"), str) or not isinstance(value.get("model"), str):
        return None
    if not value["model"] or len(value["model"].encode("utf-8")) > _MAX_MODEL_ID_BYTES:
        return None
    # Canonical YYYY-MM-DD only. `fromisoformat` alone accepts a full datetime
    # with a local UTC offset, basic format, and week dates — and since Track
    # 19A a cached `day` becomes a KEY in a synced event row, so a tampered or
    # corrupted cache could otherwise put a per-machine timezone offset on the
    # wire. Fresh parses always emit `date().isoformat()`; this bounds what a
    # cache HIT can reintroduce.
    if not _CANONICAL_DAY.fullmatch(value["day"]):
        return None
    try:
        datetime.fromisoformat(value["day"])
    except ValueError:
        return None
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return None
    if any(not _is_valid_counter(usage.get(key)) for key in TOKEN_FIELDS):
        return None
    return {
        "dev": value["dev"],
        "ino": value["ino"],
        "size": value["size"],
        "mtime_ns": value["mtime_ns"],
        "head": value["head"],
        "head_len": value["head_len"],
        "tail": value["tail"],
        "tail_len": value["tail_len"],
        "offset": value["offset"],
        "day": value["day"],
        "model": value["model"],
        "usage": {key: usage[key] for key in TOKEN_FIELDS},
    }


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_valid_counter(value: Any) -> bool:
    return _is_nonnegative_int(value) and value <= _MAX_COUNTER


def _same_cache_metadata(entry: _CacheEntry, source: os.stat_result) -> bool:
    return (
        entry["dev"] == source.st_dev
        and entry["ino"] == source.st_ino
        and entry["size"] == source.st_size
        and entry["mtime_ns"] == source.st_mtime_ns
    )


def _same_fingerprint(entry: _CacheEntry, fingerprint: _Fingerprint) -> bool:
    return (
        entry["head"] == fingerprint.head
        and entry["head_len"] == fingerprint.head_len
        and entry["tail"] == fingerprint.tail
        and entry["tail_len"] == fingerprint.tail_len
    )


def _fingerprint(path: Path, source: os.stat_result, deadline: float) -> _Fingerprint:
    if _expired(deadline):
        raise _ReadFailure("deadline")
    head_len = min(source.st_size, _HEAD_PROBE_BYTES)
    tail_len = min(source.st_size, _TAIL_PROBE_BYTES)
    return _Fingerprint(
        _digest_range(path, 0, head_len, deadline),
        head_len,
        _digest_range(path, source.st_size - tail_len, tail_len, deadline),
        tail_len,
    )


def _digest_range(path: Path, offset: int, length: int, deadline: float) -> str:
    """Hash a bounded source range, preserving the reader's deadline."""
    if _expired(deadline):
        raise _ReadFailure("deadline")
    try:
        with path.open("rb") as fp:
            fp.seek(offset)
            data = fp.read(length)
    except OSError as exc:
        raise _ReadFailure("io_error") from exc
    if _expired(deadline):
        raise _ReadFailure("deadline")
    if len(data) != length:
        raise _ReadFailure("stale")
    return hashlib.sha256(data).hexdigest()


def _regular_stat(path: Path) -> os.stat_result:
    try:
        source = path.lstat()
    except OSError as exc:
        raise _ReadFailure("io_error") from exc
    if stat.S_ISLNK(source.st_mode) or not stat.S_ISREG(source.st_mode):
        raise _ReadFailure("stale")
    return source


def _same_source(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _cache_key(path: Path) -> str:
    """Hash the canonical path; cache data must not retain a raw local path."""
    with suppress(OSError):
        return hashlib.sha256(os.fsencode(path.resolve())).hexdigest()
    return hashlib.sha256(os.fsencode(path.absolute())).hexdigest()


def _expired(deadline: float) -> bool:
    return time.monotonic() >= deadline


def _incomplete(reason: Reason) -> HostUsageResult:
    return HostUsageResult({}, complete=False, reason=reason)


__all__ = [
    "CACHE_PATH",
    "CACHE_VERSION",
    "CODEX_SESSIONS_PATH",
    "DEFAULT_READ_BUDGET_S",
    "GROK_CACHE_PATH",
    "GROK_SESSIONS_PATH",
    "grok_sessions_root",
    "HostFamily",
    "HostTokens",
    "HostUsageResult",
    "OPENCODE_CACHE_PATH",
    "OPENCODE_DATA_PATH",
    # `Reason` is a cross-module contract since Track 19A: events_tail derives
    # its entire user-visible reason vocabulary from `get_args(Reason)`.
    "Reason",
    "host_family",
    "grok_completed_once",
    "read_codex_usage",
    "read_grok_usage",
    "read_opencode_usage",
    "warm_host_cache_inline",
]
