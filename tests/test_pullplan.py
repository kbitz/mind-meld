"""Track 91A: the pull rule (`pullplan.decide`), the ledger overlay and the planner.

`decide` is the one pure function the real apply path and the planner both call.
These tests drive it directly, one row of the rule table at a time, then through a
small machine model that applies `decide` and settles the ledger the way the real
pull does (`core.stamp_ancestry` and `core.record_published` on the push side),
so each named sequence from the plan runs against the production rule.

The rule table (docs/invariants/conflicts.md), in order:

  1. local missing        -> held (removed here on purpose) or write
  2. local == remote      -> matched
  3. remote == peer base  -> skip
  4. local clean and in remote.from -> fast-forward (older when the remote mtime is older)
  5. remote in ancestry   -> stale
  6. local clean and == peer base   -> fast-forward (older)
  7. no base, no from     -> fallback (today's rule)
  8. otherwise            -> both (union when mergeable, else conflict)
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mind_meld import core, pullplan
from mind_meld.manifest import mtime_from_manifest
from mind_meld.merge import merge_file, should_merge

SRC = "notes"
REL = "note.md"  # not mergeable
MEMORY = "memory/MEMORY.md"  # line-union mergeable
PEER = "bbbbbbbb"
OTHER = "cccccccc"
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
NOW_US = int(NOW.timestamp() * 1_000_000)
FRESH = NOW + timedelta(days=1)  # newer than every recorded row


def hours(n: float) -> datetime:
    """A moment before NOW (every ledger row in these tests is recorded at NOW)."""
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=n)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


V0, V1, V2, V3 = (digest(f"version {n}".encode()) for n in range(4))
P0, P1, P2, P3 = (pullplan.prefix(v) for v in (V0, V1, V2, V3))


def make_ledger(*, bases=None, rows=(), readable=True, untracked=frozenset(), path=REL):
    """A ledger holding `rows` for `path`: (sha prefix, kind, time_us, deleted_by)."""
    seen = {(SRC, path): list(rows)} if rows else {}
    return pullplan.Ledger(
        bases or {},
        seen,
        readable=readable,
        untracked=untracked,
        now_us=NOW_US,
    )


def held(prefix_: str, when: int = NOW_US, by: str | None = None):
    return (prefix_, pullplan.HELD, when, by)


def seen(prefix_: str, when: int = NOW_US, by: str | None = None):
    return (prefix_, pullplan.SEEN, when, by)


def base(sha: str, peer: str = PEER, path: str = REL) -> dict:
    return {(peer, SRC, path): sha}


def local(sha: str | None = None, mtime: datetime | None = None, exists: bool = True):
    return pullplan.LocalFile(exists, sha, mtime)


MISSING = pullplan.LocalFile()


def verdict(
    ledger,
    here,
    remote_sha,
    *,
    remote_mtime: datetime | None = hours(5),
    remote_from=(),
    peer=PEER,
    path=REL,
    own_lists=False,
    anchor_ok=False,
):
    return pullplan.decide(
        ledger,
        peer,
        SRC,
        path,
        here,
        remote_sha,
        remote_mtime,
        list(remote_from),
        own_lists=own_lists,
        anchor_ok=anchor_ok,
    )


# ── helpers on shas and hostile `from` lists ──────────────────────────


def test_prefix_is_sixteen_lowercase_hex_or_nothing():
    assert pullplan.prefix(V0) == V0[:16]
    assert pullplan.prefix("a" * 16) == "a" * 16
    assert pullplan.prefix("a" * 15) is None
    assert pullplan.prefix("A" * 16) is None
    assert pullplan.prefix("g" * 16) is None
    assert pullplan.prefix(None) is None
    assert pullplan.prefix(1234567890123456) is None


def test_valid_hash_is_sixty_four_lowercase_hex():
    assert pullplan.valid_hash(V0)
    assert not pullplan.valid_hash(V0[:63])
    assert not pullplan.valid_hash(V0 + "a")
    assert not pullplan.valid_hash(V0.upper())
    assert not pullplan.valid_hash("g" * 64)
    assert not pullplan.valid_hash(None)
    assert not pullplan.valid_hash(b"a" * 64)
    assert not pullplan.valid_hash(["a" * 64])


@pytest.mark.parametrize(
    "raw",
    [
        "a" * 16,  # a string, not a list
        {"a" * 16: 1},
        7,
        None,
        ["a" * 16] * 17,  # more than 16 entries
        ["a" * 16, "g" * 16],  # non-hex
        ["a" * 16, "A" * 16],  # uppercase
        ["a" * 15],  # wrong length
        ["a" * 17],
        ["a" * 16, 5],  # not a string
        ["a" * 16, None],
        [["a" * 16]],
    ],
)
def test_a_hostile_from_reads_as_absent(raw):
    assert pullplan.parse_from({"from": raw}) == []


def test_parse_from_accepts_a_well_formed_list_and_missing_info():
    full = [format(n, "016x") for n in range(16)]
    assert pullplan.parse_from({"from": full}) == full
    assert pullplan.parse_from({"from": [P0, P1]}) == [P0, P1]
    assert pullplan.parse_from({"from": []}) == []
    assert pullplan.parse_from({}) == []
    assert pullplan.parse_from(None) == []


# ── Ledger ────────────────────────────────────────────────────────────


def test_hold_is_a_no_op_for_the_newest_held_version_and_times_only_grow():
    ledger = pullplan.Ledger(now_us=NOW_US)
    ledger.hold(SRC, REL, V0)
    first = ledger.entries(SRC, REL)[P0].time
    assert first == NOW_US
    ledger.hold(SRC, REL, V0)  # newest already: nothing recorded
    assert ledger.entries(SRC, REL)[P0].time == first
    assert list(ledger.new_seen) == [(SRC, REL, P0)]

    ledger.hold(SRC, REL, V1)
    ledger.hold(SRC, REL, V2)
    times = [ledger.entries(SRC, REL)[p].time for p in (P0, P1, P2)]
    assert times == sorted(set(times)) and times[0] < times[1] < times[2]
    assert ledger.newest_held(SRC, REL) == P2

    # Holding an older version again makes it the newest again (a revert).
    ledger.hold(SRC, REL, V0)
    assert ledger.newest_held(SRC, REL) == P0
    assert ledger.held_newest_first(SRC, REL) == [P0, P2, P1]


def test_times_are_strictly_increasing_per_path_even_with_a_frozen_clock():
    ledger = pullplan.Ledger(now_us=NOW_US)
    for sha in (V0, V1, V2, V3):
        ledger.hold(SRC, REL, sha)
    ledger.see(SRC, REL, digest(b"extra"))
    all_times = [e.time for e in ledger.entries(SRC, REL).values()]
    assert len(all_times) == len(set(all_times)) == 5
    # Another path starts from the clock again.
    ledger.hold(SRC, "other.md", V0)
    assert ledger.entries(SRC, "other.md")[P0].time == NOW_US


def test_held_outranks_seen_and_seen_never_demotes():
    ledger = pullplan.Ledger(now_us=NOW_US)
    ledger.see(SRC, REL, V0)
    assert ledger.entries(SRC, REL)[P0].kind == pullplan.SEEN
    assert ledger.newest_held(SRC, REL) is None  # a seen version is ancestry, not a baseline
    ledger.hold(SRC, REL, V0)
    assert ledger.entries(SRC, REL)[P0].kind == pullplan.HELD
    assert ledger.newest_held(SRC, REL) == P0
    before = ledger.entries(SRC, REL)[P0].time
    ledger.see(SRC, REL, V0)
    assert ledger.entries(SRC, REL)[P0].kind == pullplan.HELD
    assert ledger.entries(SRC, REL)[P0].time == before


def test_see_of_a_known_version_records_nothing_new():
    ledger = pullplan.Ledger(now_us=NOW_US)
    ledger.see(SRC, REL, V0)
    ledger.mark_committed()
    ledger.see(SRC, REL, V0)
    assert not ledger.dirty


def test_a_malformed_sha_is_never_recorded():
    ledger = pullplan.Ledger(now_us=NOW_US)
    ledger.hold(SRC, REL, "not-a-sha")
    ledger.see(SRC, REL, "")
    ledger.stamp_deleted(SRC, REL, "short", OTHER)
    assert ledger.entries(SRC, REL) == {} and not ledger.dirty


def test_settle_records_the_base_and_holds_unless_told_otherwise():
    ledger = pullplan.Ledger(now_us=NOW_US)
    ledger.settle(PEER, SRC, REL, V1)
    assert ledger.base(PEER, SRC, REL) == V1
    assert ledger.newest_held(SRC, REL) == P1
    ledger.settle(PEER, SRC, REL, V2, hold=False)  # a conflict: saw it, did not build on it
    assert ledger.base(PEER, SRC, REL) == V2
    assert ledger.newest_held(SRC, REL) == P1
    assert ledger.entries(SRC, REL)[P2].kind == pullplan.SEEN
    ledger.settle(PEER, SRC, REL, V3, also_hold=(V0,))  # a union holds both sides
    assert {p for p, e in ledger.entries(SRC, REL).items() if e.kind == pullplan.HELD} == {
        P0,
        P1,
        P3,
    }
    assert ledger.new_bases == {(PEER, SRC, REL): V3}


def test_stamp_clear_stamps_and_a_held_row_keeps_its_stamp():
    ledger = pullplan.Ledger(now_us=NOW_US)
    ledger.stamp_deleted(SRC, REL, V0, OTHER)
    assert ledger.entries(SRC, REL)[P0].deleted_by == OTHER
    assert ledger.entries(SRC, REL)[P0].kind == pullplan.HELD
    assert ledger.newest_stamp(SRC, REL) == OTHER
    assert ledger.new_seen[(SRC, REL, P0)][2] == OTHER

    ledger.hold(SRC, REL, V1)  # a later, unstamped version is the newest row
    assert ledger.newest_stamp(SRC, REL) is None
    ledger.hold(SRC, REL, V0)  # the stamped version again: its stamp survives
    assert ledger.entries(SRC, REL)[P0].deleted_by == OTHER
    assert ledger.newest_stamp(SRC, REL) == OTHER

    ledger.clear_stamps(SRC, REL)
    assert ledger.entries(SRC, REL)[P0].deleted_by is None
    assert ledger.newest_stamp(SRC, REL) is None
    assert (SRC, REL) in ledger.unstamp
    assert ledger.new_seen[(SRC, REL, P0)][2] is None  # nothing pending re-stamps it

    ledger.mark_committed()
    ledger.clear_stamps(SRC, REL)  # nothing stamped: nothing to commit
    assert not ledger.dirty


def test_followed_deletion_stamp_is_newer_than_newly_seen_ancestors():
    ledger = pullplan.Ledger(now_us=NOW_US)
    ledger.settle(PEER, SRC, REL, V2, ancestors=(P1, P0))
    last_seen = max(entry.time for entry in ledger.entries(SRC, REL).values())
    ledger.mark_committed()

    ledger.stamp_deleted(SRC, REL, V2, PEER)

    assert ledger.newest_stamp(SRC, REL) == PEER
    assert ledger.entries(SRC, REL)[P2].time > last_seen
    assert ledger.new_seen[(SRC, REL, P2)][1:] == (
        ledger.entries(SRC, REL)[P2].time,
        PEER,
    )


def test_drop_many_removes_every_peer_base_and_pending_base_in_one_batch():
    removed = {(SRC, f"gone-{i}.md") for i in range(200)}
    ledger = pullplan.Ledger(
        {(peer, source, path): V0 for peer in (PEER, OTHER) for source, path in removed},
        now_us=NOW_US,
    )
    for source, path in removed:
        ledger.settle(PEER, source, path, V1)
    ledger.settle(PEER, SRC, "keep.md", V2)
    ledger.settle(OTHER, "other", "gone-0.md", V2)

    ledger.drop_many(iter(removed))

    expected = {(PEER, SRC, "keep.md"): V2, (OTHER, "other", "gone-0.md"): V2}
    assert ledger.bases == expected
    assert ledger.new_bases == expected
    assert ledger.drop_bases == removed
    assert ledger.newest_held(SRC, "gone-0.md") == P1


def test_successful_deletion_cancels_only_its_deferred_keep_local_rows():
    ledger = pullplan.Ledger(now_us=NOW_US)
    ledger.settle(PEER, SRC, REL, V0)
    ledger.deferred.extend([(OTHER, SRC, REL, V1, (P2,)), (OTHER, SRC, "keep.md", V1, (P2,))])
    ledger.drop_many([(SRC, REL)])
    ledger.stamp_deleted(SRC, REL, V0, PEER)
    ledger.mark_committed()  # deletion preparation commits before the move
    ledger.discard_deferred(SRC, REL)  # only a completed move cancels the decision
    ledger.flush_deferred()

    assert ledger.base(OTHER, SRC, REL) is None
    assert ledger.newest_stamp(SRC, REL) == PEER
    assert set(ledger.entries(SRC, REL)) == {P0}
    assert ledger.base(OTHER, SRC, "keep.md") == V1


def test_failed_deletion_keeps_the_deferred_keep_local_decision():
    ledger = pullplan.Ledger(now_us=NOW_US)
    ledger.settle(PEER, SRC, REL, V0)
    ledger.deferred.append((OTHER, SRC, REL, V1, (P2,)))
    ledger.drop_many([(SRC, REL)])
    ledger.stamp_deleted(SRC, REL, V0, PEER)
    ledger.mark_committed()
    ledger.undelete(SRC, REL, PEER, V0, restore_base=True)
    ledger.flush_deferred()

    assert ledger.base(OTHER, SRC, REL) == V1
    assert ledger.base(PEER, SRC, REL) == V0
    assert ledger.newest_stamp(SRC, REL) is None


def test_drop_forgets_every_peers_base_for_the_path_only():
    ledger = pullplan.Ledger(
        {
            (PEER, SRC, REL): V0,
            (OTHER, SRC, REL): V0,
            (PEER, SRC, "keep.md"): V0,
            (PEER, "other", REL): V0,
        },
        now_us=NOW_US,
    )
    ledger.settle(PEER, SRC, REL, V1)
    ledger.drop(SRC, REL)
    assert ledger.base(PEER, SRC, REL) is None and ledger.base(OTHER, SRC, REL) is None
    assert ledger.base(PEER, SRC, "keep.md") == V0
    assert ledger.base(PEER, "other", REL) == V0
    assert (SRC, REL) in ledger.drop_bases
    assert (PEER, SRC, REL) not in ledger.new_bases
    assert ledger.newest_held(SRC, REL) == P1  # the seen rows stay


def test_discard_undoes_every_uncommitted_change_for_a_path():
    ledger = make_ledger(rows=[held(P0)], bases=base(V0))
    ledger.settle(PEER, SRC, REL, V1)
    ledger.settle(PEER, SRC, "other.md", V1)
    ledger.discard(SRC, REL)
    assert ledger.base(PEER, SRC, REL) is None
    assert set(ledger.entries(SRC, REL)) == {P0}  # the committed row stays
    assert ledger.base(PEER, SRC, "other.md") == V1  # another path is untouched
    assert list(ledger.new_seen) == [(SRC, "other.md", P1)]


def test_a_copy_is_isolated_from_the_original():
    ledger = make_ledger(rows=[held(P0)], bases=base(V0))
    copy = ledger.copy()
    assert copy.base(PEER, SRC, REL) == V0 and copy.newest_held(SRC, REL) == P0
    assert copy.now_us == ledger.now_us and copy.readable
    copy.settle(PEER, SRC, REL, V1)
    copy.stamp_deleted(SRC, REL, V0, OTHER)
    copy.drop(SRC, REL)
    assert ledger.base(PEER, SRC, REL) == V0
    assert set(ledger.entries(SRC, REL)) == {P0}
    assert ledger.entries(SRC, REL)[P0].deleted_by is None
    assert not ledger.dirty
    # A copy starts clean: a planner's overlay never owes the database a write.
    assert not ledger.copy().dirty


def test_an_unreadable_ledger_knows_nothing_and_records_nothing():
    ledger = pullplan.Ledger(readable=False, now_us=NOW_US)
    assert ledger.base(PEER, SRC, REL) is None and ledger.entries(SRC, REL) == {}
    ledger.settle(PEER, SRC, REL, V0)
    ledger.hold(SRC, REL, V0)
    ledger.see(SRC, REL, V0)
    ledger.drop(SRC, REL)
    assert not ledger.dirty and ledger.newest_held(SRC, REL) is None
    # Everything falls back to today's rule: a differing local file takes the old path,
    # a missing one is written.
    assert verdict(ledger, local(V1, hours(1)), V0) == "fallback"
    assert verdict(ledger, MISSING, V0, own_lists=True, anchor_ok=True) == "write"


def test_untracked_sources_keep_a_base_but_no_versions():
    ledger = pullplan.Ledger(untracked=frozenset({"mm-events"}), now_us=NOW_US)
    ledger.settle(PEER, "mm-events", "a.jsonl", V0)
    ledger.stamp_deleted("mm-events", "a.jsonl", V0, OTHER)
    assert ledger.base(PEER, "mm-events", "a.jsonl") == V0
    assert ledger.entries("mm-events", "a.jsonl") == {}
    assert not any(k[0] == "mm-events" for k in ledger.new_seen)


def test_deferred_settles_apply_as_seen_only_and_only_when_flushed():
    ledger = pullplan.Ledger(now_us=NOW_US)
    ledger.deferred.append((PEER, SRC, REL, V1, ()))
    assert ledger.base(PEER, SRC, REL) is None and not ledger.dirty
    ledger.flush_deferred()
    assert ledger.base(PEER, SRC, REL) == V1
    assert ledger.entries(SRC, REL)[P1].kind == pullplan.SEEN  # kept local: not built on it
    assert ledger.deferred == []


def test_mark_committed_clears_pending_work_but_keeps_the_in_memory_state():
    ledger = pullplan.Ledger(now_us=NOW_US)
    ledger.settle(PEER, SRC, REL, V0)
    ledger.drop(SRC, "gone.md")
    ledger.flush_paths[(SRC, REL)] = [Path("x")]
    assert ledger.dirty
    ledger.mark_committed()
    assert not ledger.dirty and ledger.flush_paths == {}
    assert ledger.base(PEER, SRC, REL) == V0 and ledger.newest_held(SRC, REL) == P0


def test_flush_ledger_files_reports_every_failure_and_discards_only_unsafe_rows(monkeypatch):
    ledger = pullplan.Ledger(now_us=NOW_US)
    ledger.settle(PEER, SRC, REL, V0)
    ledger.settle(PEER, SRC, "safe.md", V1)
    bad_paths = [Path("canonical.md"), Path("conflict.md")]
    safe_path = Path("safe.md")
    ledger.flush_paths = {(SRC, REL): bad_paths, (SRC, "safe.md"): [safe_path]}
    errors, attempted = [], []
    failure = OSError("flush failed")

    def flush(path):
        attempted.append(path)
        if path in bad_paths:
            raise failure

    monkeypatch.setattr(core.fsutil, "fsync_file", flush)
    elapsed = core.flush_ledger_files(ledger, on_error=lambda path, exc: errors.append((path, exc)))

    assert isinstance(elapsed, float)
    assert attempted == [*bad_paths, safe_path]
    assert errors == [(path, failure) for path in bad_paths]
    assert ledger.base(PEER, SRC, REL) is None
    assert ledger.entries(SRC, REL) == {}
    assert ledger.base(PEER, SRC, "safe.md") == V1


@pytest.mark.parametrize("platform,count", [("linux", 500), ("linux", 501), ("darwin", 501)])
def test_flush_ledger_files_uses_sync_only_above_linux_threshold(monkeypatch, platform, count):
    ledger = pullplan.Ledger(now_us=NOW_US)
    paths = [Path(f"file-{i}.md") for i in range(count)]
    ledger.flush_paths[(SRC, REL)] = paths
    syncs, flushed = [], []
    monkeypatch.setattr(core.sys, "platform", platform)
    monkeypatch.setattr(core.os, "sync", lambda: syncs.append(True))
    monkeypatch.setattr(core.fsutil, "fsync_file", flushed.append)

    elapsed = core.flush_ledger_files(ledger)

    assert isinstance(elapsed, float)
    assert syncs == ([True] if platform == "linux" and count > 500 else [])
    assert flushed == ([] if syncs else paths)


def test_failed_linux_sync_reports_each_path_and_discards_all_settlements(monkeypatch):
    ledger = pullplan.Ledger(now_us=NOW_US)
    paths = [Path(f"file-{i}.md") for i in range(501)]
    for path in paths:
        ledger.settle(PEER, SRC, str(path), V0)
        ledger.flush_paths[(SRC, str(path))] = [path]
    failure = OSError("sync failed")
    errors = []

    def fail_sync():
        raise failure

    monkeypatch.setattr(core.sys, "platform", "linux")
    monkeypatch.setattr(core.os, "sync", fail_sync)
    elapsed = core.flush_ledger_files(ledger, on_error=lambda path, exc: errors.append((path, exc)))

    assert isinstance(elapsed, float)
    assert errors == [(path, failure) for path in paths]
    assert ledger.new_bases == {} and ledger.new_seen == {}


# ── decide: row 1, the local file is missing ──────────────────────────


def test_row1_a_fresh_machine_writes():
    assert verdict(None, MISSING, V0) == "write"
    assert verdict(pullplan.Ledger(now_us=NOW_US), MISSING, V0) == "write"
    # A version never held or seen is written whatever else is known.
    ledger = make_ledger(rows=[held(P1)])
    assert verdict(ledger, MISSING, V0, own_lists=True, anchor_ok=True) == "write"


def test_row1_removed_here_on_purpose_is_held_not_restored():
    ledger = make_ledger(rows=[held(P0)])
    assert verdict(ledger, MISSING, V0, own_lists=True, anchor_ok=True) == "held"


def test_row1_a_seen_version_counts_as_ancestry_too():
    ledger = make_ledger(rows=[seen(P0)])
    assert verdict(ledger, MISSING, V0, own_lists=True, anchor_ok=True) == "held"


def test_row1_needs_the_own_manifest_to_list_the_path_or_a_stamp():
    ledger = make_ledger(rows=[held(P0)])
    # Pulled but never published since: the file may simply not have arrived intact.
    assert verdict(ledger, MISSING, V0, own_lists=False, anchor_ok=True) == "write"
    # A followed deletion stamps the row, which stands in for the listing.
    stamped = make_ledger(rows=[held(P0, by=OTHER)])
    assert verdict(stamped, MISSING, V0, own_lists=False, anchor_ok=True) == "held"


def test_row1_writes_when_the_scan_root_changed():
    ledger = make_ledger(rows=[held(P0)])
    assert verdict(ledger, MISSING, V0, own_lists=True, anchor_ok=False) == "write"
    stamped = make_ledger(rows=[held(P0, by=OTHER)])
    assert verdict(stamped, MISSING, V0, own_lists=True, anchor_ok=False) == "write"


def test_row1_an_origins_real_restore_has_a_fresh_mtime_and_is_written():
    ledger = make_ledger(rows=[held(P0)])
    assert (
        verdict(ledger, MISSING, V0, own_lists=True, anchor_ok=True, remote_mtime=FRESH) == "write"
    )
    assert (
        verdict(ledger, MISSING, V0, own_lists=True, anchor_ok=True, remote_mtime=None) == "write"
    )


def test_row1_the_recorded_moment_itself_still_holds():
    recorded = hours(7)
    ledger = make_ledger(rows=[held(P0, when=int(recorded.timestamp() * 1_000_000))])
    kw = {"own_lists": True, "anchor_ok": True}
    assert verdict(ledger, MISSING, V0, remote_mtime=recorded, **kw) == "held"
    later = recorded + timedelta(microseconds=1)
    assert verdict(ledger, MISSING, V0, remote_mtime=later, **kw) == "write"


def test_row1_an_origin_that_restores_with_an_old_mtime_is_written_via_the_stamp():
    # `mv`, `cp -p` or Finder's Put Back keep the old mtime: the row stamped
    # `deleted_by == peer` still lets the origin's restore through.
    ledger = make_ledger(rows=[held(P0, by=PEER)])
    kw = {"own_lists": True, "anchor_ok": True, "remote_mtime": hours(1)}
    assert verdict(ledger, MISSING, V0, peer=PEER, **kw) == "write"
    # A different peer republishing the same bytes is not the origin: still held.
    assert verdict(ledger, MISSING, V0, peer=OTHER, **kw) == "held"


# ── decide: rows 2 and 3 ──────────────────────────────────────────────


def test_row2_equal_bytes_match_whatever_the_ledger_says():
    assert verdict(None, local(V0, hours(1)), V0) == "matched"
    assert verdict(make_ledger(readable=False), local(V0, hours(1)), V0) == "matched"
    ledger = make_ledger(bases=base(V1), rows=[held(P1)])
    assert verdict(ledger, local(V0, hours(1)), V0, remote_from=[P1]) == "matched"


def test_an_unknown_local_digest_is_never_equal():
    assert verdict(None, local(None, hours(1)), V0) == "fallback"
    ledger = make_ledger(bases=base(V1), rows=[held(P1)])
    assert verdict(ledger, local(None, hours(1)), V0) == "both"


def test_row3_remote_equal_to_the_peers_base_is_skipped():
    ledger = make_ledger(bases=base(V1), rows=[held(P0)])
    assert verdict(ledger, local(V0, hours(1)), V1) == "skip"
    # Even when a fast-forward would otherwise apply (rows 3 before 4).
    assert verdict(ledger, local(V0, hours(1)), V1, remote_from=[P0]) == "skip"
    # And before staleness (row 3 before 5).
    both = make_ledger(bases=base(V1), rows=[held(P0), held(P1)])
    assert verdict(both, local(V0, hours(1)), V1) == "skip"


def test_row3_is_per_peer():
    ledger = make_ledger(bases=base(V1, peer=OTHER), rows=[held(P0)])
    assert verdict(ledger, local(V0, hours(1)), V1, peer=PEER, remote_from=[P0]) == "fast-forward"


# ── decide: rows 4 and 6, fast-forward and the older hold ─────────────


def test_row4_clean_local_built_on_by_the_remote_fast_forwards():
    ledger = make_ledger(bases=base(V0), rows=[held(P0)])
    assert verdict(ledger, local(V0, hours(1)), V1, remote_from=[P0]) == "fast-forward"


def test_row4_needs_the_local_digest_in_from_and_the_newest_held_row():
    ledger = make_ledger(rows=[held(P0, when=NOW_US), held(P1, when=NOW_US + 1)])
    # local == P0 but the newest held is P1: local is not clean (it was reverted by hand).
    assert verdict(ledger, local(V0, hours(1)), V2, remote_from=[P0]) == "both"
    # local == newest held but the remote was not built on it.
    assert verdict(ledger, local(V1, hours(1)), V2, remote_from=[P0]) == "both"
    assert verdict(ledger, local(V1, hours(1)), V2, remote_from=[P1]) == "fast-forward"


def test_row4_without_any_held_row_the_peers_base_is_the_baseline():
    ledger = make_ledger(bases=base(V0))
    assert verdict(ledger, local(V0, hours(1)), V1, remote_from=[P0]) == "fast-forward"
    assert verdict(ledger, local(V2, hours(1)), V1, remote_from=[P2]) == "both"


@pytest.mark.parametrize("row", ["row4", "row6"])
def test_a_fast_forward_whose_remote_is_older_than_local_is_held_as_older(row):
    ledger = make_ledger(bases=base(V0), rows=[held(P0)])
    kw = {"remote_from": [P0]} if row == "row4" else {}
    newer_local = local(V0, hours(10))
    assert verdict(ledger, newer_local, V1, remote_mtime=hours(2), **kw) == "older"
    # Equal mtimes are not older; an unparseable or missing mtime is never older.
    assert verdict(ledger, newer_local, V1, remote_mtime=hours(10), **kw) == "fast-forward"
    assert verdict(ledger, newer_local, V1, remote_mtime=None, **kw) == "fast-forward"
    assert verdict(ledger, local(V0, None), V1, remote_mtime=hours(2), **kw) == "fast-forward"
    assert verdict(ledger, newer_local, V1, remote_mtime=hours(11), **kw) == "fast-forward"


def test_a_naive_mtime_is_never_older():
    ledger = make_ledger(bases=base(V0), rows=[held(P0)])
    naive = datetime(2026, 1, 1)  # comparing with an aware datetime raises TypeError
    assert verdict(ledger, local(V0, hours(10)), V1, remote_mtime=naive) == "fast-forward"


def test_row6_a_peer_that_publishes_no_from_still_fast_forwards_on_its_base():
    # An older release (or a first publish) carries no ancestry: local == baseline == base.
    ledger = make_ledger(bases=base(V0), rows=[held(P0)])
    assert verdict(ledger, local(V0, hours(1)), V1) == "fast-forward"


def test_row6_needs_local_to_equal_the_peers_base():
    ledger = make_ledger(bases=base(V2), rows=[held(P0)])
    # local V0 is the baseline, but the peer's base is V2 and the remote has no `from`.
    assert verdict(ledger, local(V0, hours(1)), V1) == "both"


def test_equal_to_an_old_peer_base_is_not_clean_when_the_baseline_moved_on():
    # base[PEER] == local == V0, but this machine published V1 since and reverted by hand.
    ledger = make_ledger(bases=base(V0), rows=[held(P0, when=NOW_US), held(P1, when=NOW_US + 1)])
    assert verdict(ledger, local(V0, hours(1)), V2, remote_from=[P0]) == "both"
    assert verdict(ledger, local(V0, hours(1)), V2) == "both"


# ── decide: row 5, stale ──────────────────────────────────────────────


def test_row5_a_version_this_machine_moved_past_is_stale():
    ledger = make_ledger(bases=base(V3), rows=[held(P1), held(P2, when=NOW_US + 1)])
    assert verdict(ledger, local(V2, hours(5)), V1) == "stale"


def test_row5_a_seen_version_is_stale_too():
    ledger = make_ledger(bases=base(V3), rows=[held(P2), seen(P1, when=NOW_US + 1)])
    assert verdict(ledger, local(V2, hours(5)), V1) == "stale"


def test_row4_comes_before_row5_so_an_exact_revert_with_a_fresh_mtime_propagates():
    # This machine once held V0 (so V0 is in its ancestry) and now holds V1.
    ledger = make_ledger(bases=base(V1), rows=[held(P0), held(P1, when=NOW_US + 1)])
    # The peer reverted to V0 exactly, from [V1]: a fast-forward, not stale.
    assert verdict(ledger, local(V1, hours(5)), V0, remote_from=[P1], remote_mtime=FRESH) == (
        "fast-forward"
    )
    # Without the ancestry link the same bytes are just an old version: stale.
    assert verdict(ledger, local(V1, hours(5)), V0, remote_mtime=FRESH) == "stale"


def test_row5_comes_before_row6():
    ledger = make_ledger(bases=base(V1, peer=OTHER), rows=[held(P0), held(P1, when=NOW_US + 1)])
    # local == baseline; the remote V0 is in the ancestry: stale, never a roll back.
    assert verdict(ledger, local(V1, hours(5)), V0, peer=OTHER) == "stale"


def test_a_restored_machines_older_bytes_are_held_as_older_not_applied():
    ledger = make_ledger(bases=base(V0), rows=[held(P0)])
    assert (
        verdict(ledger, local(V0, hours(10)), V1, remote_from=[P0], remote_mtime=hours(2))
        == "older"
    )


# ── decide: rows 7 and 8 ──────────────────────────────────────────────


def test_row7_no_base_and_no_ancestry_is_todays_rule():
    assert verdict(make_ledger(), local(V0, hours(1)), V1) == "fallback"
    assert verdict(pullplan.Ledger(now_us=NOW_US), local(V0, hours(1)), V1) == "fallback"
    assert verdict(None, local(V0, hours(1)), V1) == "fallback"
    # Rows for an unrelated path do not matter.
    other = make_ledger(rows=[held(P0)], path="other.md")
    assert verdict(other, local(V0, hours(1)), V1) == "fallback"


def test_row7_needs_an_empty_from():
    assert verdict(make_ledger(), local(V0, hours(1)), V1, remote_from=[P2]) == "both"


def test_row7_a_held_version_without_a_base_for_this_peer_is_still_both_changed():
    # This machine moved on (held V0 -> local is V2 by hand) and never synced with PEER.
    ledger = make_ledger(rows=[held(P0)])
    assert verdict(ledger, local(V2, hours(1)), V1) == "fallback"
    assert verdict(ledger, local(V2, hours(1)), V1, remote_from=[P3]) == "both"


def test_row8_both_changed():
    ledger = make_ledger(bases=base(V0), rows=[held(P0)])
    assert verdict(ledger, local(V3, hours(1)), V1, remote_from=[P0]) == "both"
    assert verdict(ledger, local(V3, hours(1)), V1) == "both"


def test_the_rule_rows_apply_in_order():
    # One input that satisfies rows 2..8 at once resolves to the earliest row.
    ledger = make_ledger(bases=base(V0), rows=[held(P0)])
    here = local(V0, hours(1))
    assert verdict(ledger, here, V0, remote_from=[P0]) == "matched"  # 2 over 3
    assert verdict(ledger, here, V1, remote_from=[P0]) == "fast-forward"  # 4
    seen_too = make_ledger(bases=base(V0), rows=[held(P0), seen(P1, when=NOW_US + 1)])
    assert verdict(seen_too, here, V1, remote_from=[P0]) == "fast-forward"  # 4 over 5
    assert verdict(seen_too, here, V1) == "stale"  # 5 over 6
    assert verdict(ledger, here, V1) == "fast-forward"  # 6


# ── the machine model: decide + settle as the real pull does ──────────


class Machine:
    """One machine's copy of one path, moved only through the production helpers.

    Pushing runs `core.stamp_ancestry` and `core.record_published` on a one-entry
    manifest; pulling runs `pullplan.decide` and then settles the ledger the way
    `_ApplyReporter` does for the outcome the verdict leads to.
    """

    def __init__(self, ident: str, rel: str = REL):
        self.id = ident * 8
        self.rel = rel
        self.ledger = pullplan.Ledger(now_us=NOW_US)
        self.data: bytes | None = None
        self.mtime: datetime | None = None
        self.pub: dict | None = None
        self.pub_data: bytes | None = None
        self.conflicts: list[bytes] = []
        self.older: list[bytes] = []
        self.lists = False  # does this machine's own published manifest list the path
        self.anchor_ok = True

    @property
    def sha(self) -> str | None:
        return None if self.data is None else digest(self.data)

    def edit(self, data: bytes, when: datetime) -> None:
        self.data, self.mtime = data, when

    def remove(self) -> None:
        self.data = None

    def push(self) -> None:
        assert self.data is not None and self.mtime is not None
        info = {"sha256": self.sha, "mtime": self.mtime.isoformat()}
        manifest = {"sources": {SRC: {"files": {self.rel: info}}}}
        prior = {"sources": {SRC: {"files": {self.rel: self.pub}}}} if self.pub else None
        core.stamp_ancestry(manifest, prior, self.ledger)
        core.record_published(self.ledger, manifest)
        self.pub, self.pub_data, self.lists = info, self.data, True

    def pull(self, peer: Machine) -> str:
        """Returns the rule's verdict, or the union / conflict / local-newer word for rows 7-8."""
        remote = peer.pub
        assert remote is not None
        remote_mtime = mtime_from_manifest(remote["mtime"])
        here = pullplan.LocalFile(self.data is not None, self.sha, self.mtime)
        word = pullplan.decide(
            self.ledger,
            peer.id,
            SRC,
            self.rel,
            here,
            remote["sha256"],
            remote_mtime,
            pullplan.parse_from(remote),
            own_lists=self.lists,
            anchor_ok=self.anchor_ok,
        )
        if word in ("write", "fast-forward"):
            self.data, self.mtime = peer.pub_data, remote_mtime
            self.ledger.settle(
                peer.id, SRC, self.rel, remote["sha256"], ancestors=pullplan.parse_from(remote)
            )
        elif word == "matched":
            self.ledger.settle(
                peer.id, SRC, self.rel, remote["sha256"], ancestors=pullplan.parse_from(remote)
            )
        elif word == "older":
            self.older.append(peer.pub_data)
        elif word in ("fallback", "both"):
            word = self._both(peer, remote, remote_mtime, today=word == "fallback")
        return word

    def _both(self, peer, remote, remote_mtime, *, today: bool) -> str:
        local_sha = self.sha
        if should_merge(self.rel):
            merged = merge_file(self.rel, self.data, peer.pub_data)
            if merged == self.data:
                self.ledger.settle(
                    peer.id, SRC, self.rel, remote["sha256"], ancestors=pullplan.parse_from(remote)
                )
                return "union"
            self.data, self.mtime = merged, NOW
            self.ledger.settle(
                peer.id,
                SRC,
                self.rel,
                remote["sha256"],
                also_hold=(local_sha,),
                ancestors=pullplan.parse_from(remote),
            )
            return "union"
        if today and self.mtime is not None and self.mtime > remote_mtime:
            return "local-newer"  # nothing recorded: the next pull decides again
        self.conflicts.append(peer.pub_data)
        self.ledger.settle(
            peer.id,
            SRC,
            self.rel,
            remote["sha256"],
            hold=False,
            ancestors=pullplan.parse_from(remote),
        )
        return "conflict"


def fleet(letters: str, rel: str = REL, v0: bytes = b"x\ny\n") -> dict[str, Machine]:
    """Machines that all hold `v0`, have published it and have synced it with each other."""
    machines = {letter: Machine(letter, rel) for letter in letters}
    for m in machines.values():
        m.edit(v0, hours(1))
        m.push()
    for a in machines.values():
        for b in machines.values():
            if a is not b:
                a.ledger.settle(b.id, SRC, rel, digest(v0))
    return machines


X_Y = b"x\ny\n"
X_Y_Z = b"x\ny\nz\n"
X_Z = b"x\nz\n"  # y deleted
KINDS = pytest.mark.parametrize("rel", [REL, MEMORY], ids=["plain", "mergeable"])

# A settle also sees the remote's published ancestry (`from`), so a lagging peer that
# still advertises a version this machine moved past reads as stale, not as a rival edit.
ANCESTRY_GAP = ()


@KINDS
def test_alternating_handoff_fast_forwards_although_the_base_for_the_peer_is_stale(rel):
    # A edits and pushes V1; B pulls, edits (deleting a line) and pushes V2 built on V1,
    # never publishing V1 itself. A's base for B is still V0, but A fast-forwards.
    m = fleet("AB", rel)
    a, b = m["A"], m["B"]
    a.edit(X_Y_Z, hours(10))
    a.push()
    assert b.pull(a) == "fast-forward"
    b.edit(X_Z, hours(20))
    b.push()
    assert a.ledger.base(b.id, SRC, rel) == digest(X_Y)
    assert a.pull(b) == "fast-forward"
    assert a.data == X_Z  # the deleted line stays deleted: no union
    assert a.conflicts == []
    assert a.ledger.base(b.id, SRC, rel) == digest(X_Z)


@KINDS
def test_a_stale_peer_republishing_an_older_version_is_silent(rel):
    m = fleet("AB", rel)
    a, b = m["A"], m["B"]
    a.edit(X_Y_Z, hours(10))
    a.push()
    assert b.pull(a) == "fast-forward"
    b.push()  # B republishes A's V1 and nothing newer
    a.edit(X_Z, hours(20))
    a.push()
    before = (a.data, a.mtime, a.ledger.base(b.id, SRC, rel), a.ledger.dirty)
    assert a.pull(b) == "stale"
    assert (a.data, a.mtime, a.ledger.base(b.id, SRC, rel), a.ledger.dirty) == before
    assert a.conflicts == [] and a.older == []


@KINDS
@pytest.mark.parametrize("order", [pytest.param("AB", marks=ANCESTRY_GAP), "BA"])
def test_a_lagging_peer_in_either_pull_order_leaves_the_newest_version(rel, order):
    # A: V1 then V2 (y deleted); B caught up to V1 and republished it, then lags;
    # C is at V0 and pulls both. Every order ends at V2 with no conflict and no union.
    m = fleet("ABC", rel)
    a, b, c = m["A"], m["B"], m["C"]
    a.edit(X_Y_Z, hours(10))
    a.push()
    assert b.pull(a) == "fast-forward"
    b.push()
    a.edit(X_Z, hours(20))
    a.push()
    words = [c.pull(m[p]) for p in order]
    assert c.data == X_Z, words
    assert c.conflicts == []
    assert "union" not in words and "conflict" not in words, words


@KINDS
@pytest.mark.parametrize("order", ["AB", pytest.param("BA", marks=ANCESTRY_GAP)])
def test_three_machines_a_peers_edit_built_on_anothers_reaches_a_lagging_third(rel, order):
    # A publishes V1; B builds V2 on it (deleting y). C, still at V0, pulls in either order.
    m = fleet("ABC", rel)
    a, b, c = m["A"], m["B"], m["C"]
    a.edit(X_Y_Z, hours(10))
    a.push()
    assert b.pull(a) == "fast-forward"
    b.edit(X_Z, hours(20))
    b.push()
    words = [c.pull(m[p]) for p in order]
    assert c.data == X_Z, words
    assert c.conflicts == []
    assert "union" not in words and "conflict" not in words, words


@KINDS
def test_chained_edits_across_two_machines_never_conflict(rel):
    m = fleet("AB", rel)
    a, b = m["A"], m["B"]
    lines = [b"x\ny\n"]
    for step in range(1, 7):
        writer, reader = (a, b) if step % 2 else (b, a)
        lines.append(lines[-1] + f"line{step}\n".encode())
        writer.edit(lines[-1], hours(10 * step))
        writer.push()
        assert reader.pull(writer) == "fast-forward", step
        assert reader.data == lines[-1]
    assert a.conflicts == b.conflicts == []


@KINDS
def test_chained_edits_pulled_out_of_order_catch_up_in_one_step(rel):
    # B pulls only after A published four versions in a row.
    m = fleet("AB", rel)
    a, b = m["A"], m["B"]
    data = b"x\ny\n"
    for step in range(1, 5):
        data += f"line{step}\n".encode()
        a.edit(data, hours(10 * step))
        a.push()
    assert b.pull(a) == "fast-forward" and b.data == data


@KINDS
def test_a_restored_machine_republishing_older_bytes_is_held_as_older(rel):
    m = fleet("AC", rel)
    a, c = m["A"], m["C"]
    a.edit(X_Y_Z, hours(10))
    a.push()
    assert c.pull(a) == "fast-forward"
    c.push()
    assert a.pull(c) == "matched"
    # C is restored from a backup: its file is V0 again, with V0's old mtime.
    c.edit(X_Y, hours(1))
    c.push()
    assert a.pull(c) == "older"
    assert a.data == X_Y_Z and a.older == [X_Y]
    assert a.conflicts == []


@KINDS
def test_an_exact_revert_with_a_fresh_mtime_propagates(rel):
    m = fleet("AB", rel)
    a, b = m["A"], m["B"]
    a.edit(X_Y_Z, hours(10))
    a.push()
    assert b.pull(a) == "fast-forward"
    b.push()
    assert a.pull(b) == "matched"
    a.edit(X_Y, hours(30))  # the exact V0 bytes again, with a fresh mtime
    a.push()
    assert b.pull(a) == "fast-forward"  # row 4 before row 5: V0 is in B's ancestry
    assert b.data == X_Y and b.conflicts == []


@KINDS
def test_a_deliberate_revert_against_a_newer_peer_edit_is_a_conflict(rel):
    # B and A synced V1; A publishes it, then reverts to V0 bytes by hand (not published).
    # C, at V0, edits meanwhile. A pulls C's edit: local V0 equals an old base but is not
    # the baseline, so it is an edit and both sides changed.
    m = fleet("ABC", rel)
    a, b, c = m["A"], m["B"], m["C"]
    b.edit(b"x\ny\nb\n", hours(5))
    b.push()
    assert a.pull(b) == "fast-forward"
    a.push()
    a.edit(X_Y, hours(20))  # deliberate revert to the exact V0 bytes
    c.edit(b"x\ny\nc\n", hours(30))
    c.push()
    word = a.pull(c)
    assert word == ("union" if should_merge(rel) else "conflict")
    if should_merge(rel):
        assert b"c\n" in a.data
    else:
        assert a.data == X_Y and a.conflicts == [b"x\ny\nc\n"]


@KINDS
def test_true_concurrent_edits_are_a_conflict_or_a_union(rel):
    m = fleet("AB", rel)
    a, b = m["A"], m["B"]
    b.edit(b"x\ny\nb\n", hours(10))
    b.push()
    a.edit(b"x\ny\na\n", hours(20))
    a.push()
    word = a.pull(b)
    if should_merge(rel):
        assert word == "union"
        assert set(a.data.split()) == {b"x", b"y", b"a", b"b"}
    else:
        assert word == "conflict"  # both changed: row 8 gives no local-newer skip
        assert a.data == b"x\ny\na\n" and a.conflicts == [b"x\ny\nb\n"]
    a.push()
    # B pulls the result: B's edit is an ancestor of the union, or just a conflict.
    reply = b.pull(a)
    assert reply in {"fast-forward", "conflict", "union", "older"}
    if should_merge(rel):
        assert set(b.data.split()) >= {b"x", b"y", b"a", b"b"}


@pytest.mark.parametrize("local_is_newer", [True, False])
def test_with_a_base_the_local_newer_skip_is_gone_so_the_older_edit_gets_the_conflict(
    local_is_newer,
):
    # Local-newer belongs to the fallback only: once a base exists both-changed is a conflict
    # whichever side is newer, so the peer holding the older edit is told, never silently skipped.
    m = fleet("AB")
    a, b = m["A"], m["B"]
    b.edit(b"x\ny\nb\n", hours(20))
    b.push()
    a.edit(b"x\ny\na\n", hours(40 if local_is_newer else 10))
    assert a.pull(b) == "conflict"
    assert a.conflicts == [b"x\ny\nb\n"] and a.data == b"x\ny\na\n"
    # The conflict is settled: pulling the same bytes again is a quiet skip, not another copy.
    assert a.pull(b) == "skip"
    assert len(a.conflicts) == 1


def test_no_base_no_ancestry_keeps_todays_local_newer_skip():
    a, b = Machine("A"), Machine("B")
    b.edit(b"remote\n", hours(10))
    b.push()
    a.edit(b"local\n", hours(20))
    assert a.pull(b) == "local-newer"  # fallback: row 7
    a.edit(b"local\n", hours(5))
    assert a.pull(b) == "conflict"
    assert a.data == b"local\n" and a.conflicts == [b"remote\n"]


@KINDS
def test_hand_delete_before_a_push_is_held_not_restored(rel):
    m = fleet("AB", rel)
    a, b = m["A"], m["B"]
    assert a.lists  # A's published manifest lists the path
    a.remove()
    assert a.pull(b) == "held"
    assert a.data is None


@KINDS
def test_hand_delete_is_written_back_when_the_scan_root_changed(rel):
    m = fleet("AB", rel)
    a, b = m["A"], m["B"]
    a.remove()
    a.anchor_ok = False  # the folder was recreated, restored or rolled back
    assert a.pull(b) == "write"
    assert a.data == X_Y


@KINDS
def test_a_file_this_machine_never_published_is_written_back_not_held(rel):
    m = fleet("AB", rel)
    a, b = m["A"], m["B"]
    a.lists = False  # pulled it, never listed it in a push
    a.remove()
    assert a.pull(b) == "write"


@KINDS
def test_an_origin_that_restores_a_deleted_file_reaches_a_follower(rel):
    m = fleet("AB", rel)
    a, b = m["A"], m["B"]
    # A followed B's deletion: the row is stamped with B as the origin.
    a.remove()
    a.ledger.stamp_deleted(SRC, rel, digest(X_Y), b.id)
    # B restores with the old mtime (cp -p) and republishes: the stamp lets it through.
    b.edit(X_Y, hours(1))
    b.push()
    assert a.pull(b) == "write" and a.data == X_Y


@KINDS
def test_an_origin_restore_with_a_fresh_mtime_is_written(rel):
    m = fleet("AB", rel)
    a, b = m["A"], m["B"]
    a.remove()
    b.edit(X_Y, FRESH)
    b.push()
    assert a.pull(b) == "write"


@KINDS
def test_a_dormant_peer_does_not_bring_back_a_file_this_machine_removed(rel):
    m = fleet("ABD", rel)
    a, d = m["A"], m["D"]
    a.remove()  # A deleted it by hand and has published since
    assert a.pull(d) == "held"


@KINDS
def test_a_machine_that_never_held_the_file_receives_it_from_a_dormant_peer(rel):
    # Owner item (k): the rule cannot tell "never had it" from "removed it" without a row.
    m = fleet("AD", rel)
    newcomer = Machine("N", rel)
    assert newcomer.pull(m["D"]) == "write"
    assert newcomer.data == X_Y


@KINDS
def test_a_new_machine_joins_and_takes_the_peers_file(rel):
    m = fleet("A", rel)
    m["A"].edit(X_Z, hours(10))
    m["A"].push()
    newcomer = Machine("N", rel)
    assert newcomer.pull(m["A"]) == "write"
    assert newcomer.data == X_Z
    assert newcomer.ledger.base(m["A"].id, SRC, rel) == digest(X_Z)


@KINDS
def test_a_new_machine_with_its_own_copy_takes_todays_rule_not_a_fast_forward(rel):
    m = fleet("A", rel)
    m["A"].edit(X_Z, hours(10))
    m["A"].push()
    newcomer = Machine("N", rel)
    newcomer.edit(b"x\nown\n", hours(5))
    word = newcomer.pull(m["A"])
    if should_merge(rel):
        assert word == "union" and set(newcomer.data.split()) == {b"x", b"own", b"z"}
    else:
        assert word == "conflict" and newcomer.data == b"x\nown\n"


@KINDS
def test_settling_a_fast_forward_is_idempotent(rel):
    m = fleet("AB", rel)
    a, b = m["A"], m["B"]
    a.edit(X_Y_Z, hours(10))
    a.push()
    assert b.pull(a) == "fast-forward"
    assert b.pull(a) == "matched"
    assert b.pull(a) == "matched"
    assert b.conflicts == []


def test_a_conflict_is_seen_not_held_so_it_is_never_published_in_from():
    m = fleet("AB")
    a, b = m["A"], m["B"]
    b.edit(b"x\ny\nb\n", hours(20))
    b.push()
    a.edit(b"x\ny\na\n", hours(10))
    assert a.pull(b) == "conflict"
    row = a.ledger.entries(SRC, REL)[pullplan.prefix(digest(b"x\ny\nb\n"))]
    assert row.kind == pullplan.SEEN
    a.push()
    assert pullplan.prefix(digest(b"x\ny\nb\n")) not in pullplan.parse_from(a.pub)


def test_a_union_holds_both_sides():
    m = fleet("AB", MEMORY)
    a, b = m["A"], m["B"]
    b.edit(b"x\ny\nb\n", hours(10))
    b.push()
    a.edit(b"x\ny\na\n", hours(20))
    before = a.sha
    assert a.pull(b) == "union"

    def held_now():
        rows = a.ledger.entries(SRC, MEMORY).items()
        return {p for p, e in rows if e.kind == pullplan.HELD}

    theirs = pullplan.prefix(digest(b"x\ny\nb\n"))
    assert {pullplan.prefix(before), theirs} <= held_now()
    # The merged bytes are this machine's new edit: held only once it publishes them.
    assert pullplan.prefix(a.sha) not in held_now()
    a.push()
    assert pullplan.prefix(a.sha) in held_now()
    assert theirs in pullplan.parse_from(a.pub) and pullplan.prefix(before) in pullplan.parse_from(
        a.pub
    )


# ── PullPlanner: predictions use the same rule ────────────────────────


def put(path: Path, data: bytes, when: datetime) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (when.timestamp(), when.timestamp()))
    return digest(data)


def info(data: bytes, when: datetime, **extra) -> dict:
    return {"sha256": digest(data), "size": len(data), "mtime": when.isoformat()} | extra


def predict(planner, tmp_path, rel, remote, peer=PEER, name="Peer B"):
    return planner.predict(peer, name, SRC, rel, remote, tmp_path)


def planner_for(ledger=None, own=None, sidecar_holds=None):
    return pullplan.PullPlanner(ledger, own, sidecar_holds)


def test_planner_writes_a_missing_file(tmp_path):
    p = predict(planner_for(pullplan.Ledger(now_us=NOW_US)), tmp_path, REL, info(b"r", hours(1)))
    assert (p.outcome, p.reason) == ("write", "")


def test_planner_holds_a_removed_file_with_the_rules_reason(tmp_path):
    ledger = make_ledger(rows=[held(pullplan.prefix(digest(b"r")))])
    planner = planner_for(ledger, own=lambda src, rel: (True, True))
    p = predict(planner, tmp_path, REL, info(b"r", hours(1)))
    assert (p.outcome, p.reason) == ("skip", "removed here; not restored")


def test_planner_without_an_own_lookup_writes_a_removed_file(tmp_path):
    ledger = make_ledger(rows=[held(pullplan.prefix(digest(b"r")))])
    p = predict(planner_for(ledger), tmp_path, REL, info(b"r", hours(1)))
    assert p.outcome == "write"


def test_planner_matched_skip_and_stale_are_unchanged(tmp_path):
    put(tmp_path / "a.md", b"same", hours(1))
    ledger = make_ledger(bases={(PEER, SRC, "b.md"): digest(b"theirs")}, rows=[], path="b.md")
    put(tmp_path / "b.md", b"mine", hours(1))
    put(tmp_path / "c.md", b"newer", hours(1))
    stale_ledger = pullplan.Ledger(
        seen={
            (SRC, "c.md"): [
                held(pullplan.prefix(digest(b"old"))),
                held(pullplan.prefix(digest(b"newer")), NOW_US + 1),
            ]
        },
        now_us=NOW_US,
    )
    planner = planner_for(ledger)
    assert predict(planner, tmp_path, "a.md", info(b"same", hours(1))).outcome == "unchanged"
    assert predict(planner, tmp_path, "b.md", info(b"theirs", hours(1))).outcome == "unchanged"
    other = planner_for(stale_ledger)
    assert predict(other, tmp_path, "c.md", info(b"old", hours(1))).outcome == "unchanged"


def test_planner_fast_forwards_and_the_next_peer_then_matches(tmp_path):
    put(tmp_path / REL, b"v0", hours(1))
    ledger = make_ledger(bases=base(digest(b"v0")), rows=[held(pullplan.prefix(digest(b"v0")))])
    planner = planner_for(ledger)
    remote = info(b"v1", hours(5), **{"from": [pullplan.prefix(digest(b"v0"))]})
    first = predict(planner, tmp_path, REL, remote)
    assert (first.outcome, first.reason) == ("write", "fast-forward")
    # The virtual state advanced, and the real files did not.
    assert (tmp_path / REL).read_bytes() == b"v0"
    second = predict(planner, tmp_path, REL, info(b"v1", hours(5)), peer=OTHER, name="Peer C")
    assert second.outcome == "unchanged"
    assert planner.ledger.base(OTHER, SRC, REL) == digest(b"v1")
    # The planner never touches the real ledger.
    assert ledger.base(PEER, SRC, REL) == digest(b"v0") and not ledger.dirty


def test_planner_holds_an_older_peer_edit(tmp_path):
    put(tmp_path / REL, b"v0", hours(10))
    ledger = make_ledger(bases=base(digest(b"v0")), rows=[held(pullplan.prefix(digest(b"v0")))])
    remote = info(b"v1", hours(2), **{"from": [pullplan.prefix(digest(b"v0"))]})
    p = predict(planner_for(ledger), tmp_path, REL, remote)
    assert (p.outcome, p.reason) == ("older", "peer edit has an older timestamp")


def test_planner_fallback_follows_todays_rule(tmp_path):
    put(tmp_path / "newer.md", b"local", hours(20))
    put(tmp_path / "older.md", b"local", hours(1))
    put(tmp_path / "memory" / "MEMORY.md", b"- local\n", hours(1))
    planner = planner_for(pullplan.Ledger(now_us=NOW_US))
    newer = predict(planner, tmp_path, "newer.md", info(b"peer", hours(5)))
    older = predict(planner, tmp_path, "older.md", info(b"peer", hours(5)))
    merge = predict(planner, tmp_path, MEMORY, info(b"- peer\n", hours(5)))
    assert (newer.outcome, newer.reason) == ("skip", "local newer")
    assert older.outcome == "conflict"
    assert merge.outcome == "merge"


def test_planner_both_changed_conflicts_even_when_local_is_newer(tmp_path):
    put(tmp_path / REL, b"mine", hours(20))
    ledger = make_ledger(bases=base(digest(b"v0")), rows=[held(pullplan.prefix(digest(b"v0")))])
    p = predict(planner_for(ledger), tmp_path, REL, info(b"theirs", hours(5)))
    assert p.outcome == "conflict"


def test_planner_both_changed_mergeable_merges(tmp_path):
    put(tmp_path / MEMORY, b"- mine\n", hours(20))
    ledger = make_ledger(
        bases={(PEER, SRC, MEMORY): digest(b"- v0\n")},
        rows=[held(pullplan.prefix(digest(b"- v0\n")))],
        path=MEMORY,
    )
    p = predict(planner_for(ledger), tmp_path, MEMORY, info(b"- theirs\n", hours(5)))
    assert p.outcome == "merge"


def test_planner_names_a_conflict_copy_already_kept(tmp_path):
    put(tmp_path / REL, b"mine", hours(1))
    asked = []

    def holds(src, rel, peer, sha):
        asked.append((src, rel, peer, sha))
        return True

    ledger = make_ledger(bases=base(digest(b"v0")), rows=[held(pullplan.prefix(digest(b"v0")))])
    p = predict(planner_for(ledger, sidecar_holds=holds), tmp_path, REL, info(b"theirs", hours(5)))
    assert (p.outcome, p.reason) == ("kept", "conflict copy already saved")
    assert asked == [(SRC, REL, PEER, digest(b"theirs"))]
    # A conflict is seen, never held, in the overlay.
    assert planner_for(ledger).ledger is not ledger


def test_planner_a_conflict_does_not_advance_the_virtual_file(tmp_path):
    put(tmp_path / REL, b"mine", hours(1))
    ledger = make_ledger(bases=base(digest(b"v0")), rows=[held(pullplan.prefix(digest(b"v0")))])
    planner = planner_for(ledger)
    first = predict(planner, tmp_path, REL, info(b"theirs", hours(5)))
    assert first.outcome == "conflict"
    assert planner.states[(SRC, tmp_path / REL)].sha256 == digest(b"mine")
    # The same bytes from a second peer are already seen (a conflict copy holds them).
    same = predict(planner, tmp_path, REL, info(b"theirs", hours(5)), peer=OTHER, name="Peer C")
    assert same.outcome == "unchanged"
    # Different bytes from that peer still conflict against the unchanged local file.
    other = predict(planner, tmp_path, REL, info(b"another", hours(6)), peer=OTHER, name="Peer C")
    assert other.outcome == "conflict"


def test_planner_reports_a_local_symlink_and_a_folder_collision(tmp_path):
    (tmp_path / "real").write_bytes(b"x")
    (tmp_path / "link.md").symlink_to(tmp_path / "real")
    (tmp_path / "dir.md").mkdir()
    planner = planner_for(pullplan.Ledger(now_us=NOW_US))
    link = predict(planner, tmp_path, "link.md", info(b"r", hours(1)))
    folder = predict(planner, tmp_path, "dir.md", info(b"r", hours(1)))
    assert (link.outcome, link.reason) == ("skip", "local symlink preserved")
    assert (folder.outcome, folder.reason) == ("may fail", "file/directory collision")


def test_planner_a_hostile_from_and_a_bad_mtime_do_not_break_the_prediction(tmp_path):
    put(tmp_path / REL, b"v0", hours(10))
    ledger = make_ledger(bases=base(digest(b"v0")), rows=[held(pullplan.prefix(digest(b"v0")))])
    planner = planner_for(ledger)
    hostile = info(b"v1", hours(11), **{"from": "not a list"})
    assert predict(planner, tmp_path, REL, hostile).outcome == "write"  # row 6: base-clean
    # An unparseable mtime is never older: the fast-forward still applies.
    bad = {"sha256": digest(b"v2"), "size": 2, "mtime": "yesterday-ish"}
    fresh = planner_for(ledger)
    assert predict(fresh, tmp_path, REL, bad).outcome == "write"
    # And it never holds an edit as older, even against a much newer local file.
    put(tmp_path / REL, b"v0", hours(900))
    assert predict(planner_for(ledger), tmp_path, REL, bad).outcome == "write"


def test_planner_flags_an_unreadable_local_file_as_may_fail_never_as_missing(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    path = tmp_path / REL
    put(path, b"mine", hours(1))
    path.chmod(0)
    try:
        p = predict(
            planner_for(pullplan.Ledger(now_us=NOW_US)), tmp_path, REL, info(b"r", hours(5))
        )
    finally:
        path.chmod(0o600)
    assert (p.outcome, p.reason) == ("may fail", "local file unreadable")


def test_planner_predict_delete_records_a_delete(tmp_path):
    planner = planner_for(pullplan.Ledger(now_us=NOW_US))
    p = planner.predict_delete(PEER, "Peer B", SRC, "gone.md")
    assert (p.outcome, p.device_name, p.rel_path, p.label) == (
        "delete",
        "Peer B",
        "gone.md",
        "delete",
    )
    assert planner.predictions == [p]
    assert (
        planner.predict_delete(PEER, "Peer B", SRC, "x.md", "because").label == "delete (because)"
    )


def test_planner_without_a_ledger_is_todays_planner(tmp_path):
    put(tmp_path / REL, b"mine", hours(1))
    p = predict(planner_for(), tmp_path, REL, info(b"theirs", hours(5)))
    assert p.outcome == "conflict"
    assert pullplan._predict_pull_outcome(REL, info(b"theirs", hours(5)), tmp_path) == "conflict"


# ── totals wording ────────────────────────────────────────────────────


def prediction(outcome: str, rel: str = "a.md") -> pullplan.PullPrediction:
    return pullplan.PullPrediction(PEER, "Peer B", SRC, rel, outcome)


def test_totals_says_no_changes_when_everything_is_unchanged_or_empty():
    assert pullplan.totals([]) == "No changes predicted."
    assert pullplan.totals([prediction("unchanged")] * 3) == "No changes predicted."


def test_totals_baseline_wording_is_unchanged_when_no_new_outcome_appears():
    line = pullplan.totals(
        [prediction("write"), prediction("merge"), prediction("conflict"), prediction("skip")]
    )
    assert line == "Would write 1, merge up to 1, conflict 1; 1 skipped"
    assert pullplan.totals([prediction("write"), prediction("may fail")]) == (
        "Would write 1, merge up to 0, conflict 0; 0 skipped; 1 may fail"
    )


def test_totals_names_delete_older_and_kept_only_when_non_zero():
    line = pullplan.totals(
        [prediction("write"), prediction("delete"), prediction("delete", "b.md")]
    )
    assert line == "Would write 1, merge up to 0, conflict 0; 0 skipped; 2 would be deleted"
    assert "older" not in line and "already kept" not in line

    line = pullplan.totals([prediction("older"), prediction("older", "b.md")])
    assert line.endswith("; 2 older peer edits would not be applied")
    assert "deleted" not in line

    line = pullplan.totals([prediction("kept"), prediction("kept", "b.md")])
    assert line.endswith("; 2 conflict copies already kept")

    everything = pullplan.totals(
        [prediction(o, f"{o}.md") for o in ("write", "may fail", "delete", "older", "kept")]
    )
    assert everything == (
        "Would write 1, merge up to 0, conflict 0; 0 skipped; 1 may fail; 1 would be deleted; "
        "1 older peer edits would not be applied; 1 conflict copies already kept"
    )


def test_a_deletion_only_plan_is_not_reported_as_no_changes():
    assert pullplan.totals([prediction("delete")]) != "No changes predicted."
    assert pullplan.totals([prediction("kept")]) != "No changes predicted."
    assert pullplan.totals([prediction("older")]) != "No changes predicted."
