"""Tests for the init-time storage auto-pin (Track 9A).

`_auto_pin_storage_for_icloud` runs at the END of ``mm init`` (after the
green success line) and tries `brctl download <storage_path>` so the
user's first ``mm pull`` reads resident blobs instead of blocking on
iCloud File Provider materialization. Best-effort: falls back to a
Finder right-click tip on any error; silent for non-iCloud storage
paths.

These tests pin the helper directly. Fixtures redirect
``_ICLOUD_DRIVE_ROOT`` to a tmp_path-based fake so the iCloud-vs-not
branch is deterministic on any host.
"""

from __future__ import annotations

import subprocess

import pytest

from mind_meld import cli as cli_module


@pytest.fixture
def fake_icloud_root(tmp_path, monkeypatch):
    """Redirect ``_ICLOUD_DRIVE_ROOT`` to a tmp_path subdir so tests can
    construct paths that are/aren't 'under iCloud Drive' deterministically.
    """
    root = tmp_path / "fake-icloud"
    root.mkdir()
    monkeypatch.setattr(cli_module, "_ICLOUD_DRIVE_ROOT", root)
    return root


def _make_completed(returncode: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["brctl", "download", "stub"], returncode=returncode, stdout=b"", stderr=b""
    )


class TestAutoPinStorageForIcloud:
    def test_auto_pins_icloud_storage_on_success(
        self, fake_icloud_root, monkeypatch, capsys
    ) -> None:
        """Happy path: brctl returns 0, the 'Storage pinned' line surfaces,
        brctl was invoked with the correct args."""
        storage = fake_icloud_root / "mind-meld"
        storage.mkdir()

        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            assert kwargs.get("check") is False
            assert kwargs.get("capture_output") is True
            assert kwargs.get("timeout") == 10
            return _make_completed(0)

        monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

        cli_module._auto_pin_storage_for_icloud(storage)

        assert calls == [["brctl", "download", str(storage)]], (
            "brctl must be invoked exactly once with the storage path"
        )
        out = capsys.readouterr().out
        assert "Storage pinned" in out
        assert "right-click" not in out, "success path must NOT print the Finder fallback"

    def test_skips_pin_for_non_icloud_storage(
        self, fake_icloud_root, monkeypatch, capsys, tmp_path
    ) -> None:
        """Storage path outside iCloud root: no brctl call, no notice. The
        slow-pull problem only exists for iCloud paths, so no nudge needed."""
        storage = tmp_path / "scratch" / "mm-test"
        storage.mkdir(parents=True)
        # Sanity: storage is NOT under fake_icloud_root.
        assert not str(storage).startswith(str(fake_icloud_root))

        called = []

        def fake_run(args, **kwargs):
            called.append(args)
            return _make_completed(0)

        monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

        cli_module._auto_pin_storage_for_icloud(storage)

        assert called == [], "brctl must NOT be called for non-iCloud storage"
        out = capsys.readouterr().out
        assert out.strip() == "", "non-iCloud path must produce no notice"

    def test_brctl_missing_falls_back_to_finder_notice(
        self, fake_icloud_root, monkeypatch, capsys
    ) -> None:
        """brctl not on PATH (FileNotFoundError) → Finder right-click tip."""
        storage = fake_icloud_root / "mind-meld"
        storage.mkdir()

        def boom(_args, **_kwargs):
            raise FileNotFoundError(2, "No such file or directory: 'brctl'")

        monkeypatch.setattr(cli_module.subprocess, "run", boom)

        cli_module._auto_pin_storage_for_icloud(storage)

        # Rich wraps long paths at arbitrary characters in non-TTY capture
        # mode; the meaningful UX signal is that the Finder tip surfaces,
        # not the exact path rendering.
        out = capsys.readouterr().out
        assert "right-click" in out
        assert "Keep Downloaded" in out

    def test_brctl_timeout_falls_back_to_finder_notice(
        self, fake_icloud_root, monkeypatch, capsys
    ) -> None:
        """brctl wedged on a corrupt iCloud state → TimeoutExpired → Finder tip."""
        storage = fake_icloud_root / "mind-meld"
        storage.mkdir()

        def boom(_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="brctl", timeout=10)

        monkeypatch.setattr(cli_module.subprocess, "run", boom)

        cli_module._auto_pin_storage_for_icloud(storage)

        out = capsys.readouterr().out
        assert "right-click" in out
        assert "Keep Downloaded" in out

    def test_brctl_nonzero_exit_falls_back_to_finder_notice(
        self, fake_icloud_root, monkeypatch, capsys
    ) -> None:
        """brctl returns non-zero (iCloud Drive disabled at OS level, etc.)
        → Finder tip. No 'Storage pinned' confirmation since we don't know
        the daemon actually accepted the request."""
        storage = fake_icloud_root / "mind-meld"
        storage.mkdir()

        def fake_run(_args, **_kwargs):
            return _make_completed(1)

        monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

        cli_module._auto_pin_storage_for_icloud(storage)

        out = capsys.readouterr().out
        assert "Storage pinned" not in out, (
            "non-zero exit must NOT claim success; user needs to know to act"
        )
        assert "right-click" in out
        assert "Keep Downloaded" in out


class TestInitWiring:
    """Smoke test that ``init`` actually calls ``_auto_pin_storage_for_icloud``.

    The full TestInitFlow suite already exercises init end-to-end; we just
    pin the wiring so a refactor that drops the call site fails loudly.
    """

    def test_init_calls_auto_pin_storage(self, tmp_path, monkeypatch) -> None:
        from typer.testing import CliRunner

        from mind_meld.cli import app

        runner = CliRunner()

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr("mind_meld.config.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("mind_meld.crypto.store_passphrase_in_keyring", lambda _pw: False)
        monkeypatch.setattr(
            "mind_meld.skill_link._ensure_retro_skill_links", lambda dry_run=False: None
        )
        monkeypatch.setattr(
            "mind_meld.events_tail._run_events_backfill",
            lambda config, sources, device_id: None,
        )

        calls: list = []

        def stub_pin(storage_path):
            calls.append(storage_path)

        monkeypatch.setattr("mind_meld.cli._auto_pin_storage_for_icloud", stub_pin)

        storage = tmp_path / "icloud"
        # storage path, device name, passphrase x2, claude=Y, all other sources=n
        stdin = f"{storage}\nMac A\npw123\npw123\nY\nn\nn\nn\nn\n"
        result = runner.invoke(app, ["init"], input=stdin)
        assert result.exit_code == 0, result.output

        assert len(calls) == 1, "init must call _auto_pin_storage_for_icloud exactly once"
        # full_path is computed via Path(storage_path).expanduser(); for a
        # tmp_path that's already absolute, expanduser is a no-op.
        assert calls[0] == storage


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
