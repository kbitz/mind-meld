"""Push-side regression cases found during the Track 91A implementation review."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from mind_meld import core, manifest, pullplan, statedb
from mind_meld.errors import StorageError


def anchored_pair(tmp_path, *, include_dirs=None, include_files=None):
    cfg = {"name": "notes", "type": "generic", "path": str(tmp_path)}
    cfg["include_dirs"] = ["d"] if include_dirs is None else include_dirs
    cfg["include_files"] = include_files or []
    root = "d" if cfg["include_dirs"] == ["d"] else ""
    (tmp_path / root).mkdir(exist_ok=True)
    token = "a" * 32
    manifest.write_anchor(tmp_path, root, token)
    rel = f"{root}/note.md" if root else "note.md"
    prior = {
        "sources": {"notes": {"files": {rel: {"sha256": "b" * 64}}, "root_ids": {root: token}}},
        "tombstones": {},
    }
    local = {"sources": {"notes": {"files": {}}}, "tombstones": {}}
    return cfg, local, prior, rel


def test_unreadable_ledger_blocks_new_user_records_but_preserves_carried_records(tmp_path):
    cfg, local, prior, rel = anchored_pair(tmp_path)
    carried = {
        "deleted_at": datetime.now(timezone.utc).isoformat(),
        "device_id": "peer",
        "sha256": "c" * 64,
    }
    prior["tombstones"]["notes:previous.md"] = carried
    prior["sources"]["mm-events"] = {"files": {"events/old.jsonl": {"sha256": "d" * 64}}}
    sources = [cfg, {"name": "mm-events", "path": str(tmp_path), "type": "generic"}]
    guard = core.compute_mint_guard(local, prior, sources, pullplan.Ledger(readable=False))
    records = manifest.generate_tombstones(local, prior, "me", skip_mint=guard.skip)
    assert ("notes", rel) in guard.skip
    assert records["notes:previous.md"] == carried
    assert f"notes:{rel}" not in records
    assert "mm-events:events/old.jsonl" in records


@pytest.mark.parametrize("root_ids", [None, [], {"d": None}, {"d": 1}, {"d": []}])
def test_first_anchor_is_pending_when_both_recorded_and_disk_tokens_are_missing(tmp_path, root_ids):
    cfg, _, prior, _ = anchored_pair(tmp_path)
    (tmp_path / "d" / manifest.ANCHOR_NAME).unlink()
    prior["sources"]["notes"]["root_ids"] = root_ids
    assert core.anchor_pending(prior, [cfg], {})


def test_unreadable_ledger_keeps_unchanged_published_ancestry(tmp_path):
    _, _, prior, rel = anchored_pair(tmp_path)
    prior["sources"]["notes"]["files"][rel]["from"] = ["c" * 16, "d" * 16]
    local = deepcopy(prior)
    del local["sources"]["notes"]["files"][rel]["from"]
    local["sources"]["notes"]["files"]["d/changed.md"] = {"sha256": "e" * 64}
    prior["sources"]["notes"]["files"]["d/changed.md"] = {"sha256": "f" * 64}
    core.stamp_ancestry(local, prior, pullplan.Ledger(readable=False))
    assert local["sources"]["notes"]["files"][rel]["from"] == ["c" * 16, "d" * 16]
    assert "from" not in local["sources"]["notes"]["files"]["d/changed.md"]


def test_dot_include_covers_the_whole_root_for_anchor_and_membership(tmp_path):
    cfg, local, prior, rel = anchored_pair(tmp_path, include_dirs=["."])
    assert manifest.scan_roots(cfg) == [""]
    assert manifest.scan_root_for(cfg, "nested/other.md") == ""
    assert manifest.scan_root_for(cfg, rel) == ""
    assert core.compute_mint_guard(local, prior, [cfg], pullplan.Ledger()).skip == set()
    assert core.publish_anchors(local, [cfg], {}) == []
    assert local["sources"]["notes"]["root_ids"][""] == manifest.read_anchor(tmp_path, "")
    assert not core.anchor_pending(local, [cfg], {})


def test_symlinked_source_root_can_publish_and_use_its_anchor(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    cfg, local, prior, rel = anchored_pair(real, include_dirs=[], include_files=["note.md"])
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    cfg["path"] = str(alias)
    assert core.compute_mint_guard(local, prior, [cfg], pullplan.Ledger()).skip == set()
    assert core.publish_anchors(local, [cfg], {}) == []
    assert local["sources"]["notes"]["root_ids"][""] == manifest.read_anchor(real, "")
    (real / manifest.ANCHOR_NAME).unlink()
    assert core.anchor_pending(local, [cfg], {})


@pytest.mark.parametrize("alias_root", [False, True])
def test_overlapping_sources_publish_one_generation_for_the_same_scan_root(tmp_path, alias_root):
    cfg, local, _, _ = anchored_pair(tmp_path)
    alias = tmp_path / "source-alias"
    alias.symlink_to(tmp_path / "d", target_is_directory=True)
    other = (
        {"name": "alias", "path": str(alias), "type": "generic", "include_dirs": ["."]}
        if alias_root
        else {**cfg, "name": "alias"}
    )
    local["sources"]["alias"] = {"files": {}}
    assert core.publish_anchors(local, [cfg, other], {}) == []
    other_root = "" if alias_root else "d"
    assert (
        local["sources"]["notes"]["root_ids"]["d"]
        == local["sources"]["alias"]["root_ids"][other_root]
    )
    assert not core.anchor_pending(local, [cfg, other], {})


def test_anchor_helpers_do_not_enter_descendant_links_or_nested_checkouts(tmp_path):
    outside = tmp_path / "elsewhere"
    (outside / "inner").mkdir(parents=True)
    base = tmp_path / "base"
    base.mkdir()
    (base / "linked").symlink_to(outside, target_is_directory=True)
    (base / "repo" / "inside").mkdir(parents=True)
    cfg = {
        "name": "notes",
        "path": str(base),
        "type": "generic",
        "include_dirs": ["linked", "linked/inner", "repo/inside"],
    }
    local = {"sources": {"notes": {"files": {}}}}
    nested = {"notes": {"repo"}}
    assert not core.anchor_pending(None, [cfg], nested)
    assert core.publish_anchors(local, [cfg], nested) == []
    assert "root_ids" not in local["sources"]["notes"]
    assert not list(tmp_path.rglob(manifest.ANCHOR_NAME))


@pytest.mark.parametrize("missing_hash", ["unknown", "ambiguous"])
def test_a_newer_held_version_without_one_known_full_hash_never_mints(tmp_path, missing_hash):
    cfg, local, prior, rel = anchored_pair(tmp_path)
    ledger = pullplan.Ledger()
    ledger.hold("notes", rel, "c" * 64)
    if missing_hash == "ambiguous":
        ledger.bases[("one", "notes", rel)] = "c" * 64
        ledger.bases[("two", "notes", rel)] = "c" * 16 + "d" * 48
    guard = core.compute_mint_guard(local, prior, [cfg], ledger)
    assert ("notes", rel) in guard.skip
    assert manifest.generate_tombstones(local, prior, "me", skip_mint=guard.skip) == {}


def test_newest_held_hash_is_resolved_from_all_peer_bases_without_changing_prior(tmp_path):
    cfg, local, prior, rel = anchored_pair(tmp_path)
    ledger = pullplan.Ledger()
    ledger.settle("peer", "notes", rel, "c" * 64)
    core.commit_ledger(ledger)
    loaded, exc = core.load_ledger(None)
    assert exc is None
    assert loaded.bases == {("peer", "notes", rel): "c" * 64}
    guard = core.compute_mint_guard(local, prior, [cfg], loaded)
    records = manifest.generate_tombstones(
        local, prior, "me", skip_mint=guard.skip, deleted_hashes=guard.deleted_hashes
    )
    assert records[f"notes:{rel}"]["sha256"] == "c" * 64
    assert prior["sources"]["notes"]["files"][rel]["sha256"] == "b" * 64
    with statedb.connect(read_only=True) as conn:
        assert statedb.read_all_peer_bases(conn) == loaded.bases


@pytest.mark.parametrize("bad_ids", [["d"], {"d": []}, {"d": {}}, {"d": "bad"}, {"d": "A" * 32}])
def test_a_malformed_anchor_map_never_counts_as_agreement_in_a_merge(tmp_path, bad_ids):
    _, _, prior, _ = anchored_pair(tmp_path)
    other = deepcopy(prior)
    other["sources"]["notes"]["root_ids"] = bad_ids
    merged = core._merge_manifests([prior, other])
    assert "root_ids" not in merged["sources"]["notes"]


def test_left_out_clause_attributes_each_count_and_preserves_folder_text(tmp_path):
    root = tmp_path / "is unreadable notes"
    root.mkdir()
    cfg = {"name": "notes", "path": str(root), "type": "generic", "include_dirs": ["new", "gone"]}
    (root / "new").mkdir()
    prior = {
        "sources": {
            "notes": {
                "files": {rel: {"sha256": "b" * 64} for rel in ("new/a", "new/b", "gone/c")},
                "root_ids": {"new": "a" * 32, "gone": "a" * 32},
            }
        }
    }
    guard = core.compute_mint_guard({"sources": {}}, prior, [cfg], pullplan.Ledger())
    clause = guard.clause()
    assert guard.left_out_files == 3
    assert f"2 files left out because {root / 'new'} is new" in clause
    assert f"1 file left out because {root / 'gone'} is missing" in clause
    assert "3 files left out because" not in clause
    assert "cannot be read notes" not in clause


@pytest.fixture
def push_machine(tmp_path):
    from mind_meld import crypto
    from mind_meld.storage.local import LocalBackend
    from tests.test_projectid import MEMORY_KB, PASSPHRASE, Machine

    store = tmp_path / "store"
    boot = crypto.bootstrap_crypto_init(LocalBackend(store), PASSPHRASE, MEMORY_KB)
    crypto.set_crypto_session(boot.root_salt, MEMORY_KB)
    machine = Machine(tmp_path, store, "review", "/home/review", "aaaaaaaa")
    source = machine.home / "notes"
    for root in ("d", "e"):
        (source / root).mkdir(parents=True)
    machine.config["sync"]["sources"] = [
        {"name": "notes", "type": "generic", "path": str(source), "include_dirs": ["d", "e"]}
    ]
    return machine, source


def test_left_out_history_clears_only_after_an_accepted_push(push_machine, monkeypatch):
    from mind_meld.storage.local import LocalBackend

    machine, source = push_machine
    for rel in ("d/gone.md", "d/present.md", "e/other.md"):
        (source / rel).write_text(rel)
    machine.push()
    (source / "d/gone.md").unlink()
    (source / "d" / manifest.ANCHOR_NAME).unlink()

    def rows():
        with machine.on():
            ledger, exc = core.load_ledger(None)
        assert exc is None
        return ledger.seen

    before = rows()
    machine.push(dry_run=True)
    assert rows() == before
    real_put = LocalBackend.put

    def fail_manifest(backend, key, data):
        if key.startswith("manifests/"):
            raise StorageError("isolated publication failure")
        return real_put(backend, key, data)

    with monkeypatch.context() as fault:
        fault.setattr(LocalBackend, "put", fail_manifest)
        with pytest.raises(StorageError, match="isolated publication failure"):
            machine.push()
    assert rows() == before
    result = machine.push()
    assert result.left_out_files == 1
    after = rows()
    assert ("notes", "d/gone.md") not in after
    assert after[("notes", "d/present.md")] == before[("notes", "d/present.md")]
    assert after[("notes", "e/other.md")] == before[("notes", "e/other.md")]


def test_clear_seen_removes_only_that_paths_pending_history_and_rolls_back_on_failure(
    tmp_path, monkeypatch
):
    ledger = pullplan.Ledger()
    for rel in ("gone", "present"):
        ledger.settle("peer", "notes", rel, "a" * 64)
    assert core.commit_ledger(ledger) is None
    ledger.deferred = [
        ("peer", "notes", "gone", "b" * 64, ()),
        ("peer", "notes", "present", "b" * 64, ()),
    ]
    ledger.see("notes", "gone", "c" * 64)
    ledger.clear_seen("notes", "gone")
    assert ledger.entries("notes", "gone") == {}
    assert ledger.deferred == [("peer", "notes", "present", "b" * 64, ())]
    assert ledger.base("peer", "notes", "gone") == "a" * 64
    real_commit = statedb.commit_ledger

    def fail_after_changes(*args, **kwargs):
        real_commit(*args, **kwargs)
        raise statedb.StateDBError("isolated rollback")

    with monkeypatch.context() as fault:
        fault.setattr(statedb, "commit_ledger", fail_after_changes)
        assert core.commit_ledger(ledger) is not None
    loaded, exc = core.load_ledger(None)
    assert exc is None and loaded.entries("notes", "gone")
    assert core.commit_ledger(ledger) is None
    loaded, exc = core.load_ledger(None)
    assert exc is None and not loaded.entries("notes", "gone")
    assert loaded.entries("notes", "present")


@pytest.mark.parametrize("command", ["push", "autopush"])
def test_repeated_anchor_changes_are_reported_cached_and_reset_by_a_stable_noop(
    push_machine, command
):
    import json

    from mind_meld import syncdiag

    machine, source = push_machine
    (source / "d/note.md").write_text("unchanged")
    machine.push()
    manifest.write_anchor(source, "d", "b" * 32)
    first = machine.push()
    assert first.anchor_warnings == []
    with machine.on():
        first_records = syncdiag.read_observations()["push"]["records"]
    assert next(r for r in first_records if r["root"] == "d")["count"] == 1
    manifest.write_anchor(source, "d", "c" * 32)
    second = machine.cli(command)
    assert second.exit_code == 0, second.output
    with machine.on():
        records = syncdiag.read_observations()["push"]["records"]
    assert [(r["root"], r["state"], r["count"]) for r in records] == [
        ("d", "mismatch", 2),
        ("e", "stable", 0),
    ]
    if command == "autopush":
        crumb = json.loads((machine.sidecar_dir / "last-autorun.json").read_text())["push"]
        assert crumb["outcome"] == "degraded"
        warning = crumb["detail"]
    else:
        warning = second.output
        assert str(source / "d") in warning
    assert "external restore" in warning and "then mm push" not in warning
    assert machine.push() is None
    with machine.on():
        records = syncdiag.read_observations()["push"]["records"]
    assert next(r for r in records if r["root"] == "d")["count"] == 0
    manifest.write_anchor(source, "d", "d" * 32)
    assert machine.push().anchor_warnings == []


def test_anchor_observation_counter_ignores_previews_failed_publications_and_absent_files(
    push_machine, monkeypatch
):
    from mind_meld import syncdiag
    from mind_meld.storage.local import LocalBackend

    machine, source = push_machine
    note = source / "d/note.md"
    note.write_text("original")
    machine.push()
    manifest.write_anchor(source, "d", "b" * 32)
    with machine.on():
        before = syncdiag.cache_path().read_bytes()
    machine.push(dry_run=True)
    with machine.on():
        assert syncdiag.cache_path().read_bytes() == before
    real_put = LocalBackend.put

    def fail_manifest(backend, key, data):
        if key.startswith("manifests/"):
            raise StorageError("isolated publication failure")
        return real_put(backend, key, data)

    with monkeypatch.context() as fault:
        fault.setattr(LocalBackend, "put", fail_manifest)
        with pytest.raises(StorageError):
            machine.push()
    with machine.on():
        assert syncdiag.cache_path().read_bytes() == before
    assert machine.push().anchor_warnings == []
    note.unlink()
    manifest.write_anchor(source, "d", "c" * 32)
    assert machine.push().anchor_warnings == []
    with machine.on():
        records = syncdiag.read_observations()["push"]["records"]
    current = next(r for r in records if r["root"] == "d")
    assert current["state"] == "absent" and current["count"] == 0
