"""A storage folder that behaves like a Syncthing share mid-sync (Track 87A).

One shared helper for every test of the Syncthing-tolerant read path, so a
scenario is written once and reused: content that arrives late, a manifest that
arrives before its blobs, a file removed right after it was listed, and a file
that is gone or replaced at the moment it is opened.

`SyncthingFolder` is a real `LocalBackend` over a real directory. Nothing in
mind-meld is mocked; the helper only changes what the filesystem looks like at
the instant a read happens. Its retry clock is injected, so no test sleeps: each
requested delay is appended to `sleeps`, and an `on_sleep` hook can make a file
reappear during a retry.

    folder = SyncthingFolder(tmp_path / "storage")
    folder.put(key, data)
    folder.hide(key)                 # exists on the peer, not delivered yet
    folder.arrive(key)               # delivered
    folder.vanish_after_listing(key) # listed, then removed before the open
    folder.vanish_at_open(key, reappear_after=2)  # gone for two retries, then back
    folder.swap_at_open(key, new)    # replaced atomically just before the open
    folder.vanish_copy_at_validation(name)  # a conflict copy gone when it is checked
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mind_meld.storage.local import LocalBackend


class SyncthingFolder(LocalBackend):
    def __init__(self, root: str | Path) -> None:
        self.sleeps: list[float] = []
        self.listings: list[str] = []
        self._hidden: dict[str, bytes] = {}
        self._remove_after_listing: set[str] = set()
        self._gone_at_open: dict[str, int | None] = {}
        self._reappear: dict[str, list] = {}
        self._swap_at_open: dict[str, bytes] = {}
        self._vanish_at_validation: set[str] = set()
        super().__init__(root, sleep=self._record_sleep)

    # ── scenario controls ────────────────────────────────────────────

    def hide(self, key: str) -> None:
        """Take `key` out of the folder: it exists on a peer, not here yet."""
        path = self.root / key
        self._hidden[key] = path.read_bytes()
        path.unlink()

    def arrive(self, key: str) -> None:
        """Deliver a hidden `key`."""
        self.put(key, self._hidden.pop(key))

    def vanish_after_listing(self, key: str) -> None:
        """Remove `key` right after `list_keys` returns it."""
        self._remove_after_listing.add(key)

    def vanish_at_open(self, key: str, *, reappear_after: int | None = None) -> None:
        """Make `key` absent when `get` opens it. With `reappear_after=n` it is
        back after n retry sleeps (a replacement that finishes); with None it
        stays gone (a removal)."""
        self._gone_at_open[key] = reappear_after

    def swap_at_open(self, key: str, data: bytes) -> None:
        """Replace `key` atomically just before `get` opens it."""
        self._swap_at_open[key] = data

    def vanish_copy_at_validation(self, file_name: str) -> None:
        """Remove the conflict copy named `file_name` as the conflict scan
        validates it (the file is gone by the time anyone reads it)."""
        self._vanish_at_validation.add(file_name)

    # ── hooks into the backend ───────────────────────────────────────

    def find_conflict_copies(
        self, key: str, is_valid: Callable[[Path], bool] | None = None
    ) -> list[Path]:
        if is_valid is not None and self._vanish_at_validation:
            inner = is_valid

            def validate(path: Path) -> bool:
                if path.name in self._vanish_at_validation:
                    self._vanish_at_validation.discard(path.name)
                    path.unlink(missing_ok=True)
                return inner(path)

            return super().find_conflict_copies(key, validate)
        return super().find_conflict_copies(key, is_valid)

    def list_keys(self, prefix: str) -> list[str]:
        self.listings.append(prefix)
        keys = super().list_keys(prefix)
        for key in keys:
            if key in self._remove_after_listing:
                self._remove_after_listing.discard(key)
                (self.root / key).unlink(missing_ok=True)
        return keys

    def get(self, key: str, *, expected: bool = False) -> bytes:
        if key in self._swap_at_open:
            self.put(key, self._swap_at_open.pop(key))
        if key in self._gone_at_open:
            reappear_after = self._gone_at_open.pop(key)
            path = self.root / key
            if path.exists():
                if reappear_after is not None:
                    self._reappear[key] = [path.read_bytes(), reappear_after]
                path.unlink()
        return super().get(key, expected=expected)

    def _record_sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        for key in list(self._reappear):
            data, remaining = self._reappear[key]
            if remaining <= 1:
                del self._reappear[key]
                self.put(key, data)
            else:
                self._reappear[key][1] = remaining - 1
