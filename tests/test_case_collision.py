"""Pull-time case-collision detection (Group 7 preflight #6 + D11).

On case-insensitive local FS (APFS default, NTFS), two manifest entries
that casefold to the same key would resolve to the same on-disk inode.
The detection runs after exclude/disabled filtering, before the per-source
download loop; emits `mm: warning:` per cluster and drops all-but-lex-first
from each peer manifest.

Codex outside-voice T5 noted that Linux peers can legitimately have both
casings — this filter is consumer-side, not a manifest-key normalization.
The raw manifests stay intact for cross-platform peers.
"""

from __future__ import annotations

import sys

import pytest

from mind_meld.cli import (
    _detect_case_insensitive_fs,
    _detect_pull_case_collisions,
    _drop_case_collisions_from_manifests,
)


class TestDetectCaseInsensitiveFs:
    def test_returns_false_on_nonexistent_path(self, tmp_path):
        assert _detect_case_insensitive_fs(tmp_path / "nope") is False

    def test_returns_false_on_alpha_free_basename(self, tmp_path):
        """A path whose basename has no alphabetic chars can't be case-mangled."""
        d = tmp_path / "12345"
        d.mkdir()
        assert _detect_case_insensitive_fs(d) is False

    @pytest.mark.skipif(sys.platform != "darwin", reason="APFS-default needed")
    def test_apfs_default_is_case_insensitive(self, tmp_path):
        """tmp_path on macOS APFS default = case-insensitive."""
        d = tmp_path / "Projects"
        d.mkdir()
        assert _detect_case_insensitive_fs(d) is True

    def test_truly_case_sensitive_returns_false(self, tmp_path):
        """If the swapcase variant doesn't exist (case-sensitive FS), False."""
        # On case-sensitive FS, "Projects" exists but "pROJECTS" does not.
        # On case-insensitive FS, both resolve to the same inode.
        d = tmp_path / "Projects"
        d.mkdir()
        result = _detect_case_insensitive_fs(d)
        # Result depends on the test environment's FS; we can't assert
        # a fixed value cross-platform. What we CAN assert: if the alt-
        # case variant doesn't exist on disk, the function returns False.
        if not (tmp_path / "pROJECTS").exists():
            assert result is False


class TestDetectPullCaseCollisions:
    def _make_local_sources_map(self, base_path):
        return {"src1": {"path": base_path, "type": "generic"}}

    @pytest.mark.skipif(sys.platform != "darwin", reason="APFS-default needed")
    def test_collision_on_apfs(self, tmp_path):
        base = tmp_path / "src1"
        base.mkdir()
        manifest_cache = {
            "deviceA": {
                "sources": {
                    "src1": {
                        "files": {
                            "Projects/notes.md": {"sha256": "a"},
                            "config.yaml": {"sha256": "b"},
                        }
                    }
                }
            },
            "deviceB": {
                "sources": {
                    "src1": {
                        "files": {
                            "projects/notes.md": {"sha256": "c"},
                        }
                    }
                }
            },
        }
        local_sources = self._make_local_sources_map(base)
        collisions = _detect_pull_case_collisions(manifest_cache, local_sources)
        assert "src1" in collisions
        clusters = collisions["src1"]
        # One collision cluster keyed by casefold('Projects/notes.md').
        assert len(clusters) == 1
        cluster = next(iter(clusters.values()))
        assert sorted(cluster) == ["Projects/notes.md", "projects/notes.md"]

    def test_no_collisions_when_paths_differ(self, tmp_path):
        base = tmp_path / "src1"
        base.mkdir()
        manifest_cache = {
            "deviceA": {
                "sources": {"src1": {"files": {"a.md": {"sha256": "x"}, "b.md": {"sha256": "y"}}}}
            },
        }
        local_sources = self._make_local_sources_map(base)
        collisions = _detect_pull_case_collisions(manifest_cache, local_sources)
        assert collisions == {}

    def test_no_collisions_on_case_sensitive_fs(self, tmp_path, monkeypatch):
        """On case-sensitive FS, _detect_case_insensitive_fs returns False
        and the collision detector skips the source entirely. This is
        forced via monkeypatch so the test runs deterministically."""
        from mind_meld import cli as cli_module

        base = tmp_path / "src1"
        base.mkdir()
        monkeypatch.setattr(cli_module, "_detect_case_insensitive_fs", lambda _p: False)

        manifest_cache = {
            "deviceA": {
                "sources": {
                    "src1": {
                        "files": {
                            "Projects/notes.md": {"sha256": "a"},
                            "projects/notes.md": {"sha256": "c"},
                        }
                    }
                }
            },
        }
        local_sources = self._make_local_sources_map(base)
        collisions = _detect_pull_case_collisions(manifest_cache, local_sources)
        assert collisions == {}, "Linux peers can legitimately have both casings"

    def test_skips_source_not_in_local_map(self, tmp_path, monkeypatch):
        """Manifests with sources not configured locally don't produce
        collisions — the local FS detection wouldn't apply."""
        from mind_meld import cli as cli_module

        monkeypatch.setattr(cli_module, "_detect_case_insensitive_fs", lambda _p: True)

        manifest_cache = {
            "deviceA": {
                "sources": {
                    "unknown-src": {
                        "files": {
                            "X.md": {"sha256": "a"},
                            "x.md": {"sha256": "c"},
                        }
                    }
                }
            },
        }
        # Local source map has nothing matching "unknown-src".
        local_sources = {"src1": {"path": tmp_path, "type": "generic"}}
        collisions = _detect_pull_case_collisions(manifest_cache, local_sources)
        assert collisions == {}


class TestDropCaseCollisionsFromManifests:
    def test_drops_all_but_lex_first(self):
        manifest_cache = {
            "deviceA": {
                "sources": {
                    "src1": {
                        "files": {
                            "Projects/notes.md": {"sha256": "a"},
                            "config.yaml": {"sha256": "b"},
                        }
                    }
                }
            },
            "deviceB": {
                "sources": {
                    "src1": {
                        "files": {
                            "projects/notes.md": {"sha256": "c"},
                        }
                    }
                }
            },
        }
        # Lex-sort puts "Projects/notes.md" first (uppercase P < lowercase p).
        collisions = {"src1": {"projects/notes.md": ["Projects/notes.md", "projects/notes.md"]}}
        new_cache = _drop_case_collisions_from_manifests(manifest_cache, collisions)
        # Lex-first kept on deviceA; deviceB's colliding entry dropped.
        assert "Projects/notes.md" in new_cache["deviceA"]["sources"]["src1"]["files"]
        assert "config.yaml" in new_cache["deviceA"]["sources"]["src1"]["files"]
        assert "projects/notes.md" not in new_cache["deviceB"]["sources"]["src1"]["files"]

    def test_no_collisions_returns_input_unchanged(self):
        manifest_cache = {"d1": {"sources": {"s": {"files": {"a": {}}}}}}
        result = _drop_case_collisions_from_manifests(manifest_cache, {})
        assert result is manifest_cache

    def test_does_not_mutate_input(self):
        original = {
            "d1": {"sources": {"src1": {"files": {"X": {"sha256": "a"}, "x": {"sha256": "b"}}}}}
        }
        collisions = {"src1": {"x": ["X", "x"]}}
        _drop_case_collisions_from_manifests(original, collisions)
        # Original still has both entries — the function returns a deep copy.
        assert "X" in original["d1"]["sources"]["src1"]["files"]
        assert "x" in original["d1"]["sources"]["src1"]["files"]

    def test_handles_none_manifests(self):
        manifest_cache = {
            "d1": None,
            "d2": {"sources": {"src1": {"files": {"X": {}, "x": {}}}}},
        }
        collisions = {"src1": {"x": ["X", "x"]}}
        result = _drop_case_collisions_from_manifests(manifest_cache, collisions)
        assert result["d1"] is None
        assert "x" not in result["d2"]["sources"]["src1"]["files"]
