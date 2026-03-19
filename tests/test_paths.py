"""Tests for memsync.paths — cross-machine path rewriting."""

from memsync.paths import rewrite_manifest_paths, rewrite_path


class TestRewritePath:
    def test_mac_to_linux(self):
        path_map = {"/Users/kb": "/home/kb"}
        result = rewrite_path(
            "projects/-Users-kb-code-myapp/sessions/abc.json",
            path_map,
        )
        assert result == "projects/-home-kb-code-myapp/sessions/abc.json"

    def test_linux_to_mac(self):
        path_map = {"/home/kb": "/Users/kb"}
        result = rewrite_path(
            "projects/-home-kb-code-myapp/sessions/abc.json",
            path_map,
        )
        assert result == "projects/-Users-kb-code-myapp/sessions/abc.json"

    def test_no_match(self):
        path_map = {"/Users/alice": "/home/alice"}
        result = rewrite_path(
            "projects/-Users-kb-code/sessions/abc.json",
            path_map,
        )
        assert result == "projects/-Users-kb-code/sessions/abc.json"

    def test_empty_path_map(self):
        result = rewrite_path("projects/-foo/bar.json", {})
        assert result == "projects/-foo/bar.json"

    def test_multiple_mappings(self):
        path_map = {
            "/Users/kb": "/home/kb",
            "/opt/projects": "/srv/projects",
        }
        result = rewrite_path(
            "projects/-Users-kb-code/sessions/abc.json",
            path_map,
        )
        assert result == "projects/-home-kb-code/sessions/abc.json"


class TestRewriteManifestPaths:
    def test_rewrites_all_paths(self):
        files = {
            "projects/-Users-kb-app1/sessions/a.json": {"sha256": "aaa"},
            "projects/-Users-kb-app2/sessions/b.json": {"sha256": "bbb"},
        }
        path_map = {"/Users/kb": "/home/kb"}
        result = rewrite_manifest_paths(files, path_map)

        assert "projects/-home-kb-app1/sessions/a.json" in result
        assert "projects/-home-kb-app2/sessions/b.json" in result
        assert len(result) == 2

    def test_empty_path_map_returns_same(self):
        files = {"a.json": {"sha256": "aaa"}}
        result = rewrite_manifest_paths(files, {})
        assert result is files  # Same object, not a copy
